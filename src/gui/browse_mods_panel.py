"""
Browse Mods panel — displays Trending / Latest Added / Latest Updated mods
from Nexus Mods for the currently selected game.

Fetches mod lists via the Nexus v1 REST API.  The API already returns full
mod info objects so no per-mod enrichment is needed (unlike Tracked/Endorsed).

Each row shows the mod name, author, version, download count, endorsement
count, and a brief summary.  **View** opens the Nexus page; **Install**
triggers the standard download-and-install flow.  Right-click also offers
**Track Mod** and **Endorse Mod**.
"""

from __future__ import annotations

import io
import threading
import tkinter as tk
import webbrowser
from dataclasses import dataclass
from typing import Callable, Optional

import requests
from PIL import Image as PilImage, ImageTk

# ---------------------------------------------------------------------------
# Theme constants (kept in sync with gui.py)
# ---------------------------------------------------------------------------
BG_DEEP    = "#1a1a1a"
BG_PANEL   = "#252526"
BG_HEADER  = "#2a2a2b"
BG_ROW     = "#2d2d2d"
BG_ROW_ALT = "#303030"
BG_HOVER   = "#094771"
ACCENT     = "#0078d4"
ACCENT_HOV = "#1084d8"
TEXT_MAIN  = "#d4d4d4"
TEXT_DIM   = "#858585"

FONT_NORMAL = ("Segoe UI", 12)
FONT_SMALL  = ("Segoe UI", 10)

ROW_H      = 48
BTN_COL_W  = 150
VIEW_W     = 60
INSTALL_W  = 70
BTN_GAP    = 4
NAME_PAD_L = 10
HOVER_PREVIEW_MAX_W = 320
HOVER_PREVIEW_MAX_H = 200


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


def _truncate(widget, text: str, font, max_px: int) -> str:
    """Return *text* truncated with '…' so it fits within *max_px* pixels."""
    if not text:
        return ""
    if widget.tk.call("font", "measure", font, text) <= max_px:
        return text
    ellipsis = "…"
    ew = widget.tk.call("font", "measure", font, ellipsis)
    while text and widget.tk.call("font", "measure", font, text) + ew > max_px:
        text = text[:-1]
    return text + ellipsis


class BrowseModsPanel:
    """
    Canvas-based panel listing browseable mods from Nexus (Trending / Latest
    Added / Latest Updated).

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
        self._hover_idx: int = -1
        self._canvas_w: int = 400
        self._view_btns: list[tk.Button] = []
        self._install_btns: list[tk.Button] = []
        self._loading: bool = False
        self._cat_idx: int = 0  # index into CATEGORIES
        self._page: int = 0
        self._search_active: bool = False
        self._search_page: int = 0
        self._selected_cat_names: list[str] = []
        self._categories_cache: dict[str, list] = {}
        self._active_game_domain: str = ""
        self._hover_preview_win: tk.Toplevel | None = None
        self._hover_preview_label: tk.Label | None = None
        self._hover_preview_image: ImageTk.PhotoImage | None = None
        self._hover_preview_cache: dict[str, ImageTk.PhotoImage] = {}
        self._hover_preview_loading: set[str] = set()
        self._hover_preview_url: str = ""
        self._hover_preview_size: tuple[int, int] = (0, 0)
        self._row_bounds: list[tuple[int, int]] = []
        self._btn_win_ids: list[tuple[int, int]] = []  # (view_id, install_id)

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

        # Category cycle button — hidden when only one visible category
        self._cat_btn = tk.Button(
            toolbar, text=f"▸ {self._visible_categories[0][0]}",
            bg=BG_HEADER, fg=TEXT_MAIN, activebackground=BG_HOVER,
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

        # Right-side buttons packed BEFORE status label so label is clipped first
        self._next_btn = tk.Button(
            toolbar, text="Next",
            bg="#e07b00", fg="#ffffff", activebackground="#c46a00",
            activeforeground="#ffffff",
            relief="flat", font=FONT_SMALL,
            bd=0, highlightthickness=0, cursor="hand2", state="disabled",
            command=self._go_next,
        )
        self._next_btn.pack(side="right", padx=8, pady=2)

        self._prev_btn = tk.Button(
            toolbar, text="Prev",
            bg="#e07b00", fg="#ffffff", activebackground="#c46a00",
            activeforeground="#ffffff",
            relief="flat", font=FONT_SMALL,
            bd=0, highlightthickness=0, cursor="hand2", state="disabled",
            command=self._go_prev,
        )
        self._prev_btn.pack(side="right", padx=0, pady=2)

        self._status_label = tk.Label(
            toolbar, text="Click Refresh to browse mods", anchor="w",
            font=FONT_SMALL, fg=TEXT_DIM, bg=BG_HEADER,
        )
        self._status_label.pack(side="left", padx=4, fill="x", expand=True)

        # Canvas + scrollbar
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
        )
        self._canvas.configure(yscrollcommand=self._vsb.set)
        self._canvas.grid(row=0, column=0, sticky="nsew")
        self._vsb.grid(row=0, column=1, sticky="ns")

        self._canvas.bind("<Configure>",       self._on_resize)
        self._canvas.bind("<Button-4>",        lambda e: self._scroll(-24))
        self._canvas.bind("<Button-5>",        lambda e: self._scroll(24))
        self._canvas.bind("<MouseWheel>",      self._on_mousewheel)
        self._canvas.bind("<Motion>",          self._on_motion)
        self._canvas.bind("<Leave>",           self._on_leave)
        self._canvas.bind("<ButtonRelease-3>", self._on_right_click)
        self._canvas.bind("<Button-1>",        lambda e: self._canvas.focus_set())

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
    # Category cycling
    # ------------------------------------------------------------------

    def _cycle_category(self):
        """Cycle to the next browse category and auto-refresh."""
        self._cat_idx = (self._cat_idx + 1) % len(self._visible_categories)
        self._page = 0
        label, _ = self._visible_categories[self._cat_idx]
        self._cat_btn.configure(text=f"▸ {label}")
        self._update_pager()
        self.refresh()

    def _go_prev(self):
        if self._page > 0:
            self._page -= 1
            self._update_pager()
            self.refresh()

    def _go_next(self):
        self._page += 1
        self._update_pager()
        self.refresh()

    def _update_pager(self, result_count: int = 10):
        """Enable/disable Prev & Next based on current category and page."""
        is_top = self._visible_categories[self._cat_idx][1] == "get_top_mods" and not self._search_active
        can_prev = is_top and self._page > 0
        can_next = is_top and result_count >= 10
        self._prev_btn.configure(
            state="normal" if can_prev else "disabled",
            bg="#e07b00" if can_prev else BG_HEADER,
            fg="#ffffff" if can_prev else TEXT_DIM,
        )
        self._next_btn.configure(
            state="normal" if can_next else "disabled",
            bg="#e07b00" if can_next else BG_HEADER,
            fg="#ffffff" if can_next else TEXT_DIM,
        )

    def _sync_game_domain(self, domain: str) -> None:
        """Reset per-game selection state when switching the active game."""
        if not domain:
            return
        if domain == self._active_game_domain:
            return
        self._active_game_domain = domain
        self._selected_cat_names = []
        self._cat_filter_btn.configure(bg="#2d7a2d", fg="#ffffff")

    # ------------------------------------------------------------------
    # Refresh
    # ------------------------------------------------------------------

    def refresh(self):
        """Fetch mods from the Nexus API for the current category."""
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
        cat_label, cat_method = self._visible_categories[self._cat_idx]
        self._status_label.configure(text=f"Loading {cat_label}…")

        page = self._page

        def _worker():
            try:
                cat_names = self._selected_cat_names or None
                if cat_method == "get_top_mods":
                    mod_infos = api.get_top_mods(domain, offset=page * 10,
                                                  category_names=cat_names)
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

                def _done():
                    self._hide_hover_preview()
                    self._entries = entries
                    self._loading = False
                    self._refresh_btn.configure(state="normal")
                    page_info = f" — page {page + 1}" if cat_method == "get_top_mods" else ""
                    self._status_label.configure(
                        text=f"{len(entries)} {cat_label.lower()} mod(s) for {domain}{page_info}"
                    )
                    self._update_pager(len(entries))
                    self._rebuild_buttons()
                    self._repaint()
                    self._log(f"Browse: Loaded {len(entries)} {cat_label.lower()} mod(s) for {domain}{page_info}.")

                self._parent.after(0, _done)

            except Exception as exc:
                def _err(exc=exc):
                    self._loading = False
                    self._refresh_btn.configure(state="normal")
                    self._status_label.configure(text="Error")
                    self._log(f"Browse: Failed — {exc}")
                self._parent.after(0, _err)

        threading.Thread(target=_worker, daemon=True).start()

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    def _do_search(self):
        """Search mods by name for the current game."""
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
        self._search_page = 0
        self._loading = True
        self._search_btn.configure(state="disabled")
        self._status_label.configure(text=f"Searching '{query_text}'…")
        self._update_pager(0)

        def _worker():
            try:
                cat_names = self._selected_cat_names or None
                mod_infos = api.search_mods(domain, query_text,
                                             category_names=cat_names)
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
                    self._hide_hover_preview()
                    self._entries = entries
                    self._loading = False
                    self._search_btn.configure(state="normal")
                    self._status_label.configure(
                        text=f"{len(entries)} result(s) for '{query_text}'"
                    )
                    self._rebuild_buttons()
                    self._repaint()
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
        """Clear the search box and return to category browse view."""
        self._hide_hover_preview()
        self._search_var.set("")
        self._search_active = False
        self._entries = []
        self._rebuild_buttons()
        self._repaint()
        self._update_pager(0)
        cat_label, _ = self._visible_categories[self._cat_idx]
        self._status_label.configure(text=f"▸ {cat_label} — click Refresh")

    # ------------------------------------------------------------------
    # Category filter dialog
    # ------------------------------------------------------------------

    def _open_category_dialog(self):
        """Open the category filter window, fetching categories if not cached."""
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
        """Display the category selection Toplevel."""
        win = tk.Toplevel(self._parent)
        win.title("Filter by Category")
        win.configure(bg=BG_PANEL)
        win.geometry("360x500")
        win.resizable(False, True)

        # Header
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

        # Scrollable checkbox area
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

        # Build ordered list: parent categories, then their children indented
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

        # Footer
        footer = tk.Frame(win, bg=BG_HEADER)
        footer.pack(fill="x", side="bottom")

        def _apply():
            self._selected_cat_names = [
                name for name, v in check_vars.items() if v.get()
            ]
            self._cat_filter_btn.configure(
                fg="#ffffff",
                bg="#2d7a2d",
            )
            win.destroy()
            self.refresh()

        def _clear_all():
            for v in check_vars.values():
                v.set(False)
            active_lbl.configure(text="all categories", fg=TEXT_DIM)

        def _select_all():
            for v in check_vars.values():
                v.set(True)
            active_lbl.configure(
                text=f"{len(check_vars)} selected", fg=ACCENT)

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
    # Button management
    # ------------------------------------------------------------------

    def _measure_wrapped_height(self, text: str, max_px: int) -> int:
        if not text:
            return 0
        line_h = int(self._canvas.tk.call("font", "metrics", FONT_SMALL, "-linespace"))
        paragraphs = [p.strip() for p in text.splitlines()] or [text]
        line_count = 0
        for para in paragraphs:
            if not para:
                line_count += 1
                continue
            words = para.split()
            current = ""
            for w in words:
                candidate = w if not current else f"{current} {w}"
                if self._canvas.tk.call("font", "measure", FONT_SMALL, candidate) <= max_px:
                    current = candidate
                else:
                    if current:
                        line_count += 1
                    current = w
            if current:
                line_count += 1
        return max(1, line_count) * line_h

    def _build_row_layout(self) -> tuple[list[tuple[int, int]], int]:
        cw = self._canvas_w
        btn_left = cw - BTN_COL_W
        name_max_px = max(btn_left - NAME_PAD_L - 8, 20)
        title_h = int(self._canvas.tk.call("font", "metrics", FONT_NORMAL, "-linespace"))
        bounds: list[tuple[int, int]] = []
        y_top = 0
        for entry in self._entries:
            sub_parts = []
            if entry.author:
                sub_parts.append(f"by {entry.author}")
            if entry.summary:
                sub_parts.append(f"— {entry.summary}")
            full_sub = " ".join(sub_parts).strip()
            sub_h = self._measure_wrapped_height(full_sub, name_max_px) if full_sub else 0
            row_h = max(ROW_H, title_h + sub_h + 20)
            y_bot = y_top + row_h
            bounds.append((y_top, y_bot))
            y_top = y_bot
        return bounds, y_top

    def _row_index_from_canvas_y(self, y: int) -> int:
        for idx, (top, bot) in enumerate(self._row_bounds):
            if top <= y < bot:
                return idx
        return -1

    def _rebuild_buttons(self):
        """Destroy old buttons and create View + Install per entry."""
        for btn in self._view_btns:
            btn.destroy()
        for btn in self._install_btns:
            btn.destroy()
        self._view_btns.clear()
        self._install_btns.clear()
        self._btn_win_ids.clear()
        self._canvas.delete("btns")

        for entry in self._entries:
            url = f"https://www.nexusmods.com/{entry.domain_name}/mods/{entry.mod_id}"
            view_btn = tk.Button(
                self._canvas,
                text="View",
                bg=ACCENT,
                fg="#ffffff",
                activebackground=ACCENT_HOV,
                activeforeground="#ffffff",
                relief="flat",
                font=FONT_SMALL,
                bd=0,
                highlightthickness=0,
                cursor="hand2",
                command=lambda u=url: webbrowser.open(u),
            )
            install_btn = tk.Button(
                self._canvas,
                text="Install",
                bg="#2d7a2d",
                fg="#ffffff",
                activebackground="#3a9e3a",
                activeforeground="#ffffff",
                relief="flat",
                font=FONT_SMALL,
                bd=0,
                highlightthickness=0,
                cursor="hand2",
                command=lambda e=entry: self._install_fn(e),
            )
            self._view_btns.append(view_btn)
            self._install_btns.append(install_btn)

    def _place_buttons(self):
        """Reposition buttons to match current row layout.

        Creates canvas-window items once; on subsequent calls only moves them
        with coords() so the widgets are never destroyed/recreated during
        scroll (which would cause visible flickering).
        """
        cw = self._canvas_w
        install_cx = cw - INSTALL_W // 2 - 4
        view_cx = install_cx - INSTALL_W // 2 - BTN_GAP - VIEW_W // 2
        btn_h = ROW_H - 14
        n = min(len(self._view_btns), len(self._row_bounds))

        # Fast path: move existing items — no widget destroy/recreate, no flicker.
        # Validate first ID to catch stale IDs after canvas.delete("all").
        if (len(self._btn_win_ids) == n and n > 0
                and self._canvas.coords(self._btn_win_ids[0][0])):
            for row in range(n):
                y_top, _y_bot = self._row_bounds[row]
                y_center = y_top + ROW_H // 2
                v_id, i_id = self._btn_win_ids[row]
                self._canvas.coords(v_id, view_cx, y_center)
                self._canvas.coords(i_id, install_cx, y_center)
        else:
            # Slow path: (re)create items and record their IDs
            self._canvas.delete("btns")
            self._btn_win_ids.clear()
            for row in range(n):
                y_top, _y_bot = self._row_bounds[row]
                y_center = y_top + ROW_H // 2
                v_id = self._canvas.create_window(
                    view_cx, y_center,
                    window=self._view_btns[row],
                    width=VIEW_W,
                    height=btn_h,
                    tags="btns",
                )
                i_id = self._canvas.create_window(
                    install_cx, y_center,
                    window=self._install_btns[row],
                    width=INSTALL_W,
                    height=btn_h,
                    tags="btns",
                )
                self._btn_win_ids.append((v_id, i_id))

    # ------------------------------------------------------------------
    # Drawing
    # ------------------------------------------------------------------

    def _repaint(self):
        """Redraw row backgrounds and text."""
        self._canvas.delete("bg")
        self._canvas.delete("txt")

        cw = self._canvas_w
        entries = self._entries
        row_bounds, total_h = self._build_row_layout()
        self._row_bounds = row_bounds

        canvas_top = int(self._canvas.canvasy(0))
        canvas_bottom = canvas_top + self._canvas.winfo_height()

        btn_left = cw - BTN_COL_W
        name_max_px = btn_left - NAME_PAD_L - 8

        for row, entry in enumerate(entries):
            y_top, y_bot = row_bounds[row]

            if y_bot < canvas_top or y_top > canvas_bottom:
                continue

            # Row background
            if row == self._hover_idx:
                bg = BG_HOVER
            elif row % 2 == 0:
                bg = BG_ROW
            else:
                bg = BG_ROW_ALT

            self._canvas.create_rectangle(
                0, y_top, cw, y_bot, fill=bg, outline="", tags="bg",
            )

            # Primary text: mod name + version + downloads + endorsements
            title = entry.name
            if entry.version:
                title += f"  v{entry.version}"
            if entry.downloads_total:
                title += f"  ↓{entry.downloads_total:,}"
            if entry.endorsement_count:
                title += f"  ♥{entry.endorsement_count:,}"
            title = _truncate(self._canvas, title, FONT_NORMAL, max(name_max_px, 20))
            self._canvas.create_text(
                NAME_PAD_L, y_top + 14,
                text=title, anchor="w",
                font=FONT_NORMAL, fill=TEXT_MAIN, tags="txt",
            )

            # Secondary text: author + summary
            sub_parts = []
            if entry.author:
                sub_parts.append(f"by {entry.author}")
            if entry.summary:
                sub_parts.append(f"— {entry.summary}")
            sub_text = " ".join(sub_parts).strip()
            if sub_text:
                self._canvas.create_text(
                    NAME_PAD_L, y_top + 34,
                    text=sub_text, anchor="nw",
                    width=max(name_max_px, 20),
                    font=FONT_SMALL, fill=TEXT_DIM, tags="txt",
                )

        self._canvas.configure(scrollregion=(0, 0, cw, max(total_h, 1)))
        self._place_buttons()

    # ------------------------------------------------------------------
    # Scrolling
    # ------------------------------------------------------------------

    def _scroll(self, units: int):
        self._canvas.yview_scroll(units, "units")
        self._repaint()

    def _on_mousewheel(self, event):
        direction = -1 if event.delta > 0 else 1
        self._scroll(direction * 24)

    def _on_resize(self, event):
        new_w = event.width
        if new_w == self._canvas_w:
            return
        self._canvas_w = new_w
        self._canvas.delete("all")
        self._btn_win_ids.clear()  # canvas items gone after delete("all")
        self._repaint()

    # ------------------------------------------------------------------
    # Hover
    # ------------------------------------------------------------------

    def _ensure_hover_preview_window(self):
        if self._hover_preview_win and self._hover_preview_win.winfo_exists():
            return
        win = tk.Toplevel(self._parent)
        win.overrideredirect(True)
        win.attributes("-topmost", True)
        win.configure(bg=BG_PANEL)
        lbl = tk.Label(
            win,
            bg=BG_PANEL,
            bd=0,
            highlightthickness=0,
        )
        lbl.pack(padx=2, pady=2)
        win.withdraw()
        self._hover_preview_win = win
        self._hover_preview_label = lbl

    def _position_hover_preview(self, root_x: int, root_y: int):
        if not self._hover_preview_win:
            return
        w, h = self._hover_preview_size
        if w <= 0 or h <= 0:
            w, h = HOVER_PREVIEW_MAX_W, HOVER_PREVIEW_MAX_H

        screen_w = self._parent.winfo_screenwidth()
        screen_h = self._parent.winfo_screenheight()
        pad = 12

        x = root_x - w - pad
        if x < pad:
            x = root_x + pad
        if x + w + pad > screen_w:
            x = max(pad, screen_w - w - pad)

        y = root_y - h // 2
        if y < pad:
            y = pad
        if y + h + pad > screen_h:
            y = max(pad, screen_h - h - pad)

        self._hover_preview_win.geometry(f"+{int(x)}+{int(y)}")

    def _set_hover_preview_image(self, photo: ImageTk.PhotoImage, root_x: int, root_y: int):
        self._ensure_hover_preview_window()
        if not self._hover_preview_label or not self._hover_preview_win:
            return
        self._hover_preview_image = photo
        self._hover_preview_size = (photo.width(), photo.height())
        self._hover_preview_label.configure(image=photo)
        self._hover_preview_label.image = photo
        self._position_hover_preview(root_x, root_y)
        self._hover_preview_win.deiconify()

    def _show_hover_preview(self, idx: int, root_x: int, root_y: int):
        if idx < 0 or idx >= len(self._entries):
            self._hide_hover_preview()
            return

        entry = self._entries[idx]
        url = (entry.picture_url or "").strip()
        if not url:
            self._hide_hover_preview()
            return

        if url == self._hover_preview_url and self._hover_preview_image is not None:
            self._position_hover_preview(root_x, root_y)
            if self._hover_preview_win:
                self._hover_preview_win.deiconify()
            return

        self._hover_preview_url = url
        cached = self._hover_preview_cache.get(url)
        if cached is not None:
            self._set_hover_preview_image(cached, root_x, root_y)
            return

        self._hide_hover_preview(clear_url=False)
        if url in self._hover_preview_loading:
            return
        self._hover_preview_loading.add(url)

        def _worker():
            photo = None
            try:
                resp = requests.get(url, timeout=10)
                resp.raise_for_status()
                img = PilImage.open(io.BytesIO(resp.content)).convert("RGB")
                if hasattr(PilImage, "Resampling"):
                    resample = PilImage.Resampling.LANCZOS
                else:
                    resample = PilImage.LANCZOS
                img.thumbnail((HOVER_PREVIEW_MAX_W, HOVER_PREVIEW_MAX_H), resample)
                photo = ImageTk.PhotoImage(img)
            except Exception:
                photo = None

            def _done():
                self._hover_preview_loading.discard(url)
                if self._hover_preview_url != url:
                    return
                if photo is None:
                    self._hide_hover_preview()
                    return
                self._hover_preview_cache[url] = photo
                px = self._parent.winfo_pointerx()
                py = self._parent.winfo_pointery()
                self._set_hover_preview_image(photo, px, py)

            self._parent.after(0, _done)

        threading.Thread(target=_worker, daemon=True).start()

    def _hide_hover_preview(self, clear_url: bool = True):
        self._hover_preview_image = None
        self._hover_preview_size = (0, 0)
        if clear_url:
            self._hover_preview_url = ""
        if self._hover_preview_win and self._hover_preview_win.winfo_exists():
            self._hover_preview_win.withdraw()

    def _on_motion(self, event):
        y = int(self._canvas.canvasy(event.y))
        new_idx = self._row_index_from_canvas_y(y)
        if new_idx != self._hover_idx:
            self._hover_idx = new_idx
            self._repaint()
        self._show_hover_preview(new_idx, event.x_root, event.y_root)

    def _on_leave(self, _event):
        self._hide_hover_preview()
        if self._hover_idx != -1:
            self._hover_idx = -1
            self._repaint()

    # ------------------------------------------------------------------
    # Right-click context menu
    # ------------------------------------------------------------------

    def _on_right_click(self, event):
        y = int(self._canvas.canvasy(event.y))
        idx = self._row_index_from_canvas_y(y)
        if idx < 0 or idx >= len(self._entries):
            return

        entry = self._entries[idx]
        url = f"https://www.nexusmods.com/{entry.domain_name}/mods/{entry.mod_id}"

        menu = tk.Menu(
            self._canvas, tearoff=0,
            bg=BG_PANEL, fg=TEXT_MAIN,
            activebackground=ACCENT,
            activeforeground=TEXT_MAIN,
            font=FONT_SMALL,
        )
        menu.add_command(label="Open on Nexus", command=lambda: webbrowser.open(url))
        menu.add_command(label="Install Mod",
                         command=lambda e=entry: self._install_fn(e))
        menu.add_separator()
        menu.add_command(label="Track Mod",
                         command=lambda e=entry: self._track_mod(e))
        menu.add_command(label="Endorse Mod",
                         command=lambda e=entry: self._endorse_mod(e))
        menu.tk_popup(event.x_root, event.y_root)

    def _track_mod(self, entry: BrowseModEntry):
        """Track a mod via the Nexus API."""
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
        """Endorse a mod via the Nexus API."""
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
