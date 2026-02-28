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
import webbrowser
from dataclasses import dataclass
from typing import Callable, Optional

import customtkinter as ctk

from gui.ctk_components import CTkPopupMenu
from gui.mod_card import ModCard, CARD_W, CARD_PAD, CARD_COLS
from gui.theme import (
    BG_DEEP,
    BG_PANEL,
    BG_HEADER,
    BG_ROW,
    ACCENT,
    ACCENT_HOV,
    TEXT_MAIN,
    TEXT_DIM,
    FONT_NORMAL,
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


class BrowseModsPanel:
    """
    Card-grid panel listing browseable mods from Nexus (Trending / Latest
    Added / Latest Updated / Top Downloaded).

    Built into an existing parent widget (a tab frame from CTkTabview).
    """

    def __init__(
        self,
        parent_tab: tk.Widget,
        log_fn: Optional[Callable] = None,
        get_api: Optional[Callable] = None,
        get_game_domain: Optional[Callable] = None,
        install_fn: Optional[Callable] = None,
        visible_categories: Optional[list[tuple[str, str]]] = None,
    ):
        self._parent = parent_tab
        self._log = log_fn or (lambda msg: None)
        self._get_api = get_api or (lambda: None)
        self._get_game_domain = get_game_domain or (lambda: "")
        self._install_fn = install_fn or (lambda entry: None)
        self._visible_categories = visible_categories if visible_categories is not None else VISIBLE_CATEGORIES

        self._entries: list[BrowseModEntry] = []
        self._cards: list[ModCard] = []
        self._loading: bool = False
        self._cat_idx: int = 0
        self._page: int = 0
        self._search_active: bool = False
        self._selected_cat_names: list[str] = []
        self._categories_cache: dict[str, list] = {}
        self._active_game_domain: str = ""
        self._img_cache: dict[str, ctk.CTkImage] = {}
        self._img_loading: set[str] = set()
        self._cols: int = CARD_COLS
        self._context_menu: CTkPopupMenu | None = None

        self._build(parent_tab)

    # ------------------------------------------------------------------
    # Build UI
    # ------------------------------------------------------------------

    def _build(self, tab):
        tab.grid_rowconfigure(1, weight=1)
        tab.grid_rowconfigure(2, weight=0)
        tab.grid_columnconfigure(0, weight=1)

        # Toolbar
        toolbar = tk.Frame(tab, bg=BG_HEADER, height=28)
        toolbar.grid(row=0, column=0, sticky="ew")
        toolbar.grid_propagate(False)

        # Category cycle button
        self._cat_btn = tk.Button(
            toolbar, text=f"▸ {self._visible_categories[0][0]}",
            bg=BG_HEADER, fg=TEXT_MAIN, activebackground=BG_ROW,
            relief="flat", font=FONT_SMALL,
            bd=0, cursor="hand2",
            command=self._cycle_category,
        )
        if len(self._visible_categories) > 1:
            self._cat_btn.pack(side="left", padx=8, pady=2)

        self._refresh_btn = tk.Button(
            toolbar, text="↺ Refresh",
            bg=ACCENT, fg="#ffffff", activebackground=ACCENT_HOV,
            activeforeground="#ffffff",
            relief="flat", font=FONT_SMALL,
            bd=0, highlightthickness=0, cursor="hand2",
            command=self.refresh,
        )
        self._refresh_btn.pack(side="left", padx=4, pady=2)

        self._cat_filter_btn = tk.Button(
            toolbar, text="Categories",
            bg="#2d7a2d", fg="#ffffff", activebackground="#3a9e3a",
            activeforeground="#ffffff",
            relief="flat", font=FONT_SMALL,
            bd=0, highlightthickness=0, cursor="hand2",
            command=self._open_category_dialog,
        )
        self._cat_filter_btn.pack(side="left", padx=4, pady=2)

        self._prev_btn = tk.Button(
            toolbar, text="Previous",
            bg="#c37800", fg="#ffffff", activebackground="#e28b00",
            activeforeground="#ffffff",
            relief="flat", font=FONT_SMALL,
            bd=0, highlightthickness=0, cursor="hand2",
            command=self._go_prev_page,
            state="disabled",
        )
        self._prev_btn.pack(side="left", padx=4, pady=2)

        self._next_btn = tk.Button(
            toolbar, text="Next",
            bg="#c37800", fg="#ffffff", activebackground="#e28b00",
            activeforeground="#ffffff",
            relief="flat", font=FONT_SMALL,
            bd=0, highlightthickness=0, cursor="hand2",
            command=self._go_next_page,
            state="disabled",
        )
        self._next_btn.pack(side="left", padx=4, pady=2)

        self._status_label = tk.Label(
            toolbar, text="Click Refresh to browse mods", anchor="w",
            font=FONT_SMALL, fg=TEXT_DIM, bg=BG_HEADER,
        )
        self._status_label.pack(side="left", padx=4, fill="x", expand=True)

        # Scrollable card area using canvas + inner CTk frame
        canvas_frame = tk.Frame(tab, bg=BG_DEEP, bd=0, highlightthickness=0)
        canvas_frame.grid(row=1, column=0, sticky="nsew")
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

        # Inner frame that holds the card grid
        self._inner = ctk.CTkFrame(self._canvas, fg_color=BG_DEEP)
        self._inner_id = self._canvas.create_window((0, 0), window=self._inner, anchor="nw")

        self._inner.bind("<Configure>", self._on_inner_configure)
        self._canvas.bind("<Configure>", self._on_canvas_configure)
        self._canvas.bind("<Button-4>",   lambda e: self._scroll(-100))
        self._canvas.bind("<Button-5>",   lambda e: self._scroll(100))
        self._canvas.bind("<MouseWheel>", self._on_mousewheel)
        self._inner.bind("<Button-4>",    lambda e: self._scroll(-100))
        self._inner.bind("<Button-5>",    lambda e: self._scroll(100))
        self._inner.bind("<MouseWheel>",  self._on_mousewheel)

        # Search bar
        search_bar = tk.Frame(tab, bg=BG_HEADER, height=30)
        search_bar.grid(row=2, column=0, sticky="ew")
        search_bar.grid_propagate(False)

        tk.Label(
            search_bar, text="Search:",
            font=FONT_SMALL, fg=TEXT_DIM, bg=BG_HEADER,
        ).pack(side="left", padx=(8, 4), pady=4)

        self._search_var = tk.StringVar()
        self._search_entry = tk.Entry(
            search_bar,
            textvariable=self._search_var,
            bg=BG_ROW, fg=TEXT_MAIN,
            insertbackground=TEXT_MAIN,
            relief="flat", font=FONT_SMALL,
            bd=2,
        )
        self._search_entry.pack(side="left", fill="x", expand=True, pady=4)
        self._search_entry.bind("<Return>",    lambda _e: self._do_search())
        self._search_entry.bind("<Control-a>", lambda _e: (self._search_entry.selection_range(0, "end"), "break")[-1])

        self._search_btn = tk.Button(
            search_bar, text="Search",
            bg=ACCENT, fg="#ffffff", activebackground=ACCENT_HOV,
            activeforeground="#ffffff",
            relief="flat", font=FONT_SMALL,
            bd=0, highlightthickness=0, cursor="hand2",
            command=self._do_search,
        )
        self._search_btn.pack(side="left", padx=(4, 4), pady=4)

        self._clear_btn = tk.Button(
            search_bar, text="✕",
            bg="#b33a3a", fg="#ffffff", activebackground="#c94848",
            activeforeground="#ffffff",
            relief="flat", font=FONT_SMALL,
            bd=0, highlightthickness=0, cursor="hand2",
            command=self._clear_search,
        )
        self._clear_btn.pack(side="left", padx=(0, 8), pady=4)

    # ------------------------------------------------------------------
    # Canvas / scroll helpers
    # ------------------------------------------------------------------

    def _on_inner_configure(self, _event=None):
        self._canvas.configure(scrollregion=self._canvas.bbox("all"))

    def _on_canvas_configure(self, event):
        self._canvas.itemconfig(self._inner_id, width=event.width)
        self._regrid_cards()

    def _scroll(self, units: int):
        self._canvas.yview_scroll(units, "units")

    def _on_mousewheel(self, event):
        direction = -1 if event.delta > 0 else 1
        self._scroll(direction * 10)

    def _bind_scroll(self, widget):
        """Recursively bind scroll events on a widget and all its children."""
        widget.bind("<Button-4>",   lambda e: self._scroll(-50), add="+")
        widget.bind("<Button-5>",   lambda e: self._scroll(50),  add="+")
        widget.bind("<MouseWheel>", self._on_mousewheel,          add="+")
        for child in widget.winfo_children():
            self._bind_scroll(child)

    # ------------------------------------------------------------------
    # Card rendering
    # ------------------------------------------------------------------

    def _clear_cards(self):
        for c in self._cards:
            c.card.destroy()
        self._cards.clear()

    def _make_card(self, entry: BrowseModEntry) -> ModCard:
        """Create a single ModCard and bind scroll events to it."""
        url = f"https://www.nexusmods.com/{entry.domain_name}/mods/{entry.mod_id}"
        card = ModCard(
            self._inner, entry,
            on_view=lambda u=url: webbrowser.open(u),
            on_install=lambda e=entry: self._install_fn(e),
            on_right_click=lambda event, e=entry, u=url: self._show_context_menu(event, e, u),
        )
        self._bind_scroll(card.card)
        return card

    def _build_cards(self):
        """Replace all cards with the current entries."""
        self._clear_cards()
        for entry in self._entries:
            self._cards.append(self._make_card(entry))
        self._regrid_cards()
        self._load_images()

    def _regrid_cards(self):
        """Place cards in a grid with the current column count."""
        # Determine padding to centre the grid
        total_card_w = self._cols * CARD_W + (self._cols - 1) * CARD_PAD
        canvas_w = self._canvas.winfo_width() or (self._cols * (CARD_W + CARD_PAD * 2))
        x_pad = max(CARD_PAD, (canvas_w - total_card_w) // 2)

        for idx, mc in enumerate(self._cards):
            col = idx % self._cols
            row = idx // self._cols
            mc.card.grid(
                row=row, column=col,
                padx=(x_pad if col == 0 else CARD_PAD // 2,
                       x_pad if col == self._cols - 1 else CARD_PAD // 2),
                pady=CARD_PAD,
                sticky="n",
            )

        # Ensure inner frame columns expand uniformly
        for c in range(self._cols):
            self._inner.grid_columnconfigure(c, weight=1)

    def _load_images(self):
        """Kick off async image loads for cards."""
        for mc in self._cards:
            mc.load_image_async(
                getattr(mc._entry, "picture_url", "") or "",
                self._img_cache,
                self._img_loading,
                self._parent,
            )

    # ------------------------------------------------------------------
    # Category cycling
    # ------------------------------------------------------------------

    def _cycle_category(self):
        self._cat_idx = (self._cat_idx + 1) % len(self._visible_categories)
        label, _ = self._visible_categories[self._cat_idx]
        self._cat_btn.configure(text=f"▸ {label}")
        self.refresh()

    def _sync_game_domain(self, domain: str) -> None:
        if not domain:
            return
        if domain == self._active_game_domain:
            return
        self._active_game_domain = domain
        self._selected_cat_names = []
        self._cat_filter_btn.configure(bg="#2d7a2d", fg="#ffffff")

    # ------------------------------------------------------------------
    # Refresh / Pagination
    # ------------------------------------------------------------------

    def refresh(self):
        """Fetch page 0 of the current category and rebuild cards."""
        self._page = 0
        self._load_page()

    def _go_prev_page(self):
        if self._page > 0 and not self._loading and not self._search_active:
            self._page -= 1
            self._load_page()

    def _go_next_page(self):
        if not self._loading and not self._search_active:
            # Only allow next if we got a full page last time (more might exist)
            if len(self._entries) >= PAGE_SIZE:
                self._page += 1
                self._load_page()

    def _load_page(self):
        """Fetch the current page and replace cards."""
        api = self._get_api()
        if api is None:
            self._log("Browse: Set your Nexus API key first.")
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
                    self._status_label.configure(
                        text=f"{cat_label}: page {page + 1}"
                    )
                    self._log(f"Browse: Loaded page {page + 1} — {len(entries)} {cat_label.lower()} mod(s).")

                self._parent.after(0, _done)

            except Exception as exc:
                def _err(exc=exc):
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
            ))
        return entries

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    def _do_search(self):
        query_text = self._search_var.get().strip()
        if not query_text:
            return
        api = self._get_api()
        if api is None:
            self._log("Browse: Set your Nexus API key first.")
            return
        domain = self._get_game_domain()
        if not domain:
            self._log("Browse: No game selected.")
            return
        self._sync_game_domain(domain)
        if self._loading:
            return

        self._search_active = True
        self._loading = True
        self._prev_btn.configure(state="disabled")
        self._next_btn.configure(state="disabled")
        self._search_btn.configure(state="disabled")
        self._status_label.configure(text=f"Searching '{query_text}'…")

        def _worker():
            try:
                cat_names = self._selected_cat_names or None
                mod_infos = api.search_mods(domain, query_text, category_names=cat_names)
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
                    ))

                def _done():
                    self._entries = entries
                    self._loading = False
                    self._search_btn.configure(state="normal")
                    self._prev_btn.configure(state="disabled")
                    self._next_btn.configure(state="disabled")
                    self._status_label.configure(
                        text=f"{len(entries)} result(s) for '{query_text}'"
                    )
                    self._build_cards()
                    self._canvas.yview_moveto(0)
                    self._log(f"Browse: {len(entries)} search result(s) for '{query_text}' in {domain}.")

                self._parent.after(0, _done)

            except Exception as exc:
                def _err(exc=exc):
                    self._loading = False
                    self._search_btn.configure(state="normal")
                    self._status_label.configure(text="Search error")
                    self._log(f"Browse: Search failed — {exc}")
                self._parent.after(0, _err)

        threading.Thread(target=_worker, daemon=True).start()

    def _clear_search(self):
        self._search_var.set("")
        self._search_active = False
        self._entries = []
        self._clear_cards()
        self._prev_btn.configure(state="disabled")
        self._next_btn.configure(state="disabled")
        cat_label, _ = self._visible_categories[self._cat_idx]
        self._status_label.configure(text=f"▸ {cat_label} — click Refresh")

    # ------------------------------------------------------------------
    # Category filter dialog
    # ------------------------------------------------------------------

    def _open_category_dialog(self):
        api = self._get_api()
        if api is None:
            self._log("Browse: Set your Nexus API key first.")
            return
        domain = self._get_game_domain()
        if not domain:
            self._log("Browse: No game selected.")
            return
        self._sync_game_domain(domain)

        if domain in self._categories_cache:
            self._show_category_dialog(domain, self._categories_cache[domain])
            return

        self._cat_filter_btn.configure(state="disabled", text="Loading…")

        def _worker():
            try:
                cats = api.get_game_categories(domain)
                self._categories_cache[domain] = cats
                def _done():
                    self._cat_filter_btn.configure(state="normal", text="Categories")
                    self._show_category_dialog(domain, cats)
                self._parent.after(0, _done)
            except Exception as exc:
                def _err(exc=exc):
                    self._cat_filter_btn.configure(state="normal", text="Categories")
                    self._log(f"Browse: Failed to load categories — {exc}")
                self._parent.after(0, _err)

        threading.Thread(target=_worker, daemon=True).start()

    def _show_category_dialog(self, domain: str, categories: list):
        win = tk.Toplevel(self._parent)
        win.title("Filter by Category")
        win.configure(bg=BG_PANEL)
        win.geometry("360x500")
        win.resizable(False, True)

        hdr = tk.Frame(win, bg=BG_HEADER)
        hdr.pack(fill="x")
        tk.Label(hdr, text="Filter by Category", font=FONT_NORMAL,
                 fg=TEXT_MAIN, bg=BG_HEADER).pack(side="left", padx=10, pady=6)
        n_active = len(self._selected_cat_names)
        active_lbl = tk.Label(
            hdr,
            text=f"{n_active} selected" if n_active else "all categories",
            font=FONT_SMALL, fg=ACCENT if n_active else TEXT_DIM, bg=BG_HEADER,
        )
        active_lbl.pack(side="right", padx=10)

        body = tk.Frame(win, bg=BG_PANEL)
        body.pack(fill="both", expand=True, padx=4, pady=4)

        canv = tk.Canvas(body, bg=BG_PANEL, bd=0, highlightthickness=0)
        vsb = tk.Scrollbar(body, orient="vertical", command=canv.yview)
        canv.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        canv.pack(side="left", fill="both", expand=True)

        inner = tk.Frame(canv, bg=BG_PANEL)
        win_id = canv.create_window((0, 0), window=inner, anchor="nw")
        inner.bind("<Configure>",
                   lambda e: canv.configure(scrollregion=canv.bbox("all")))
        canv.bind("<Configure>",
                  lambda e: canv.itemconfig(win_id, width=e.width))
        canv.bind("<Button-4>", lambda e: canv.yview_scroll(-1, "units"))
        canv.bind("<Button-5>", lambda e: canv.yview_scroll(1, "units"))

        def _on_wheel(event):
            if getattr(event, "num", None) == 4:
                canv.yview_scroll(-1, "units")
            elif getattr(event, "num", None) == 5:
                canv.yview_scroll(1, "units")
            elif getattr(event, "delta", 0):
                step = -1 if event.delta > 0 else 1
                canv.yview_scroll(step, "units")
            return "break"

        win.bind("<MouseWheel>", _on_wheel)
        win.bind("<Button-4>", _on_wheel)
        win.bind("<Button-5>", _on_wheel)
        canv.bind("<MouseWheel>", _on_wheel)
        inner.bind("<MouseWheel>", _on_wheel)

        parents = sorted(
            [c for c in categories if c.parent_category is None],
            key=lambda c: c.name,
        )
        children_map: dict[int, list] = {}
        for c in categories:
            if c.parent_category is not None:
                children_map.setdefault(c.parent_category, []).append(c)
        for kids in children_map.values():
            kids.sort(key=lambda c: c.name)

        check_vars: dict[str, tk.BooleanVar] = {}

        for p in parents:
            var = tk.BooleanVar(value=p.name in self._selected_cat_names)
            check_vars[p.name] = var
            p_btn = tk.Checkbutton(
                inner, text=p.name, variable=var,
                bg=BG_PANEL, fg=TEXT_MAIN, selectcolor=BG_ROW,
                activebackground=BG_PANEL, activeforeground=TEXT_MAIN,
                font=FONT_NORMAL, anchor="w",
                relief="flat", borderwidth=0, highlightthickness=0,
            )
            p_btn.pack(fill="x", padx=8, pady=2)
            p_btn.bind("<MouseWheel>", _on_wheel)
            p_btn.bind("<Button-4>", _on_wheel)
            p_btn.bind("<Button-5>", _on_wheel)
            for ch in children_map.get(p.category_id, []):
                cvar = tk.BooleanVar(value=ch.name in self._selected_cat_names)
                check_vars[ch.name] = cvar
                c_btn = tk.Checkbutton(
                    inner, text=ch.name, variable=cvar,
                    bg=BG_PANEL, fg=TEXT_DIM, selectcolor=BG_ROW,
                    activebackground=BG_PANEL, activeforeground=TEXT_MAIN,
                    font=FONT_NORMAL, anchor="w",
                    relief="flat", borderwidth=0, highlightthickness=0,
                )
                c_btn.pack(fill="x", padx=24, pady=2)
                c_btn.bind("<MouseWheel>", _on_wheel)
                c_btn.bind("<Button-4>", _on_wheel)
                c_btn.bind("<Button-5>", _on_wheel)

        footer = tk.Frame(win, bg=BG_HEADER)
        footer.pack(fill="x", side="bottom")

        def _apply():
            self._selected_cat_names = [
                name for name, v in check_vars.items() if v.get()
            ]
            self._cat_filter_btn.configure(fg="#ffffff", bg="#2d7a2d")
            win.destroy()
            self.refresh()

        def _clear_all():
            for v in check_vars.values():
                v.set(False)
            active_lbl.configure(text="all categories", fg=TEXT_DIM)

        def _select_all():
            for v in check_vars.values():
                v.set(True)
            active_lbl.configure(text=f"{len(check_vars)} selected", fg=ACCENT)

        tk.Button(
            footer, text="Select All",
            bg="#2d7a2d", fg="#ffffff", activebackground="#3a9e3a",
            activeforeground="#ffffff",
            relief="flat", font=FONT_SMALL, bd=0, highlightthickness=0, cursor="hand2",
            command=_select_all,
        ).pack(side="left", padx=8, pady=6)
        tk.Button(
            footer, text="Clear All",
            bg="#b33a3a", fg="#ffffff", activebackground="#c94848",
            activeforeground="#ffffff",
            relief="flat", font=FONT_SMALL, bd=0, highlightthickness=0, cursor="hand2",
            command=_clear_all,
        ).pack(side="left", padx=4, pady=6)
        tk.Button(
            footer, text="Cancel",
            bg="#b33a3a", fg="#ffffff", activebackground="#c94848",
            activeforeground="#ffffff",
            relief="flat", font=FONT_SMALL, bd=0, highlightthickness=0, cursor="hand2",
            command=win.destroy,
        ).pack(side="right", padx=8, pady=6)
        tk.Button(
            footer, text="Apply",
            bg=ACCENT, fg="#ffffff", activebackground=ACCENT_HOV,
            activeforeground="#ffffff",
            relief="flat", font=FONT_SMALL, bd=0, highlightthickness=0, cursor="hand2",
            command=_apply,
        ).pack(side="right", padx=4, pady=6)

    # ------------------------------------------------------------------
    # Right-click context menu
    # ------------------------------------------------------------------

    def _show_context_menu(self, event, entry: BrowseModEntry, url: str):
        if self._context_menu is None:
            self._context_menu = CTkPopupMenu(
                self._parent.winfo_toplevel(), width=200, title=""
            )
        menu = self._context_menu
        menu.clear()
        menu.add_command("Open on Nexus", lambda: webbrowser.open(url))
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
                    lambda: self._log(f"Browse: Track failed — {exc}"))

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
                    lambda: self._log(f"Browse: Endorse failed — {exc}"))

        threading.Thread(target=_worker, daemon=True).start()
