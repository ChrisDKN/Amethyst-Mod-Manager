"""
Base class for Nexus mod list panels (Browse, Tracked, Endorsed, Trending).

Provides the shared canvas/scroll/card-grid infrastructure so subclasses only
need to implement their refresh logic and context menu.
"""

from __future__ import annotations

import threading
import tkinter as tk
from collections import OrderedDict
from typing import Callable, Optional

import customtkinter as ctk
from Utils.xdg import open_url

from gui.ctk_components import CTkLoader, CTkPopupMenu
from gui.wheel_compat import LEGACY_WHEEL_REDUNDANT
from gui.mod_card import ModCard, CARD_W, CARD_PAD, CARD_COLS
from gui.theme import (
    BG_DEEP,
    BG_PANEL,
    BG_HEADER,
    BG_ROW,
    BORDER,
    ACCENT,
    ACCENT_HOV,
    BG_HOVER,
    TEXT_MAIN,
    TEXT_DIM,
    FONT_HEADER,
    FONT_FAMILY,
    FONT_NORMAL,
    FONT_SMALL,
    scaled,
)


_IMG_CACHE_MAX = 60  # max images kept in memory per panel


class _LRUImageCache(OrderedDict):
    """OrderedDict-based LRU cache that evicts the oldest entry when full."""

    def __setitem__(self, key, value):
        if key in self:
            self.move_to_end(key)
        super().__setitem__(key, value)
        if len(self) > _IMG_CACHE_MAX:
            self.popitem(last=False)

    def __getitem__(self, key):
        value = super().__getitem__(key)
        self.move_to_end(key)
        return value


class _NexusModListPanel:
    """
    Shared canvas/scroll/card-grid base for all Nexus mod list panels.

    Subclasses must implement:
      - refresh()           — fetch data and populate self._entries, then call _build_cards()
      - _show_context_menu(event, entry, url)  — populate and show the right-click menu
      - _build_toolbar(toolbar)  — pack toolbar-specific widgets into the given tk.Frame

    Subclasses may override:
      - _toolbar_column  — which grid column the toolbar and canvas go in (default 1)
      - _has_cat_sidebar — set True to wire up a categories side panel in column 0

    The base __init__ calls _build(parent_tab) which in turn calls
    _build_toolbar(toolbar) so subclasses must call super().__init__(...) *after*
    defining any state they need in _build_toolbar.
    """

    # Subclass can override these before calling super().__init__
    _toolbar_column: int = 0
    _has_cat_sidebar: bool = False

    def __init__(
        self,
        parent_tab: tk.Widget,
        log_fn: Optional[Callable] = None,
        get_api: Optional[Callable] = None,
        get_game_domain: Optional[Callable] = None,
        install_fn: Optional[Callable] = None,
        get_installed_mod_ids: Optional[Callable[[], set]] = None,
    ):
        self._parent = parent_tab
        self._log = log_fn or (lambda msg: None)
        self._get_api = get_api or (lambda: None)
        self._get_game_domain = get_game_domain or (lambda: "")
        self._install_fn = install_fn or (lambda entry: None)
        self._get_installed_mod_ids = get_installed_mod_ids or (lambda: set())

        self._entries: list = []
        self._cards: list[ModCard] = []
        self._loading: bool = False
        self._img_cache: _LRUImageCache = _LRUImageCache()
        self._img_loading: set[str] = set()
        self._cols: int = CARD_COLS
        self._regrid_after_id = None
        self._context_menu: CTkPopupMenu | None = None
        self._loader: CTkLoader | None = None

        self._build(parent_tab)

    # ------------------------------------------------------------------
    # Build UI
    # ------------------------------------------------------------------

    def _build(self, tab):
        col = self._toolbar_column
        tab.grid_rowconfigure(1, weight=1)
        tab.grid_columnconfigure(col, weight=1)

        if self._has_cat_sidebar:
            tab.grid_columnconfigure(0, weight=0, minsize=0)
            self._build_cat_side_panel(tab)

        # Toolbar
        toolbar = tk.Frame(tab, bg=BG_HEADER, height=28)
        toolbar.grid(row=0, column=col, sticky="ew")
        toolbar.grid_propagate(False)

        self._refresh_btn = ctk.CTkButton(
            toolbar, text="↺ Refresh", width=72, height=26,
            fg_color=ACCENT, hover_color=ACCENT_HOV, text_color="white",
            font=FONT_HEADER, command=self.refresh,
        )
        self._refresh_btn.pack(side="left", padx=8, pady=2)

        self._build_toolbar(toolbar)

        self._status_label = ctk.CTkLabel(
            toolbar, text=self._initial_status_text(), anchor="w",
            font=FONT_SMALL, text_color=TEXT_DIM, fg_color=BG_HEADER,
        )
        self._status_label.pack(side="left", padx=4, fill="x", expand=True)

        # Scrollable card area
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
        self._canvas.bind("<MouseWheel>", self._on_mousewheel)
        self._inner.bind("<MouseWheel>",  self._on_mousewheel)
        if not LEGACY_WHEEL_REDUNDANT:
            self._canvas.bind("<Button-4>", lambda e: self._scroll(-50))
            self._canvas.bind("<Button-5>", lambda e: self._scroll(50))
            self._inner.bind("<Button-4>",  lambda e: self._scroll(-50))
            self._inner.bind("<Button-5>",  lambda e: self._scroll(50))

    def _initial_status_text(self) -> str:
        """Override to set the initial status bar text."""
        return "Click Refresh to load mods"

    def _build_toolbar(self, toolbar: tk.Frame) -> None:
        """Override to pack additional widgets into the toolbar (after the Refresh button)."""
        pass

    # ------------------------------------------------------------------
    # Canvas / scroll helpers
    # ------------------------------------------------------------------

    def _on_inner_configure(self, _event=None):
        if getattr(self, "_inner_sr_syncing", False):
            return
        self._inner_sr_syncing = True
        try:
            new_sr = (0, 0, self._inner.winfo_reqwidth(), self._inner.winfo_reqheight())
            if new_sr != getattr(self, "_inner_sr_applied", None):
                self._canvas.configure(scrollregion=new_sr)
                self._inner_sr_applied = new_sr
        finally:
            self._inner_sr_syncing = False

    def _on_canvas_configure(self, event):
        if self._regrid_after_id:
            self._canvas.after_cancel(self._regrid_after_id)
        self._regrid_after_id = self._canvas.after(50, self._schedule_regrid)

    def _on_canvas_map(self, _event=None):
        if self._regrid_after_id:
            self._canvas.after_cancel(self._regrid_after_id)
        self._regrid_after_id = self._canvas.after(150, self._schedule_regrid)

    def _schedule_regrid(self):
        self._regrid_after_id = None
        self._regrid_cards()

    def _scroll(self, units: int):
        self._canvas.yview_scroll(units, "units")

    def _on_mousewheel(self, event):
        # Linux/Flatpak: event.delta is often 0, use event.num (4=up, 5=down)
        num = getattr(event, "num", None)
        delta = getattr(event, "delta", 0) or 0
        if num == 4 or delta > 0:
            direction = -1
        else:
            direction = 1
        self._scroll(direction * 50)

    def _bind_scroll(self, widget, _depth=0):
        """Bind scroll events on a widget and descendants (max 3 levels deep)."""
        widget.bind("<MouseWheel>", self._on_mousewheel,          add="+")
        if not LEGACY_WHEEL_REDUNDANT:
            widget.bind("<Button-4>", lambda e: self._scroll(-50), add="+")
            widget.bind("<Button-5>", lambda e: self._scroll(50),  add="+")
        if _depth < 3:
            for child in widget.winfo_children():
                self._bind_scroll(child, _depth + 1)

    # ------------------------------------------------------------------
    # Card rendering
    # ------------------------------------------------------------------

    def _clear_cards(self):
        for c in self._cards:
            c.card.destroy()
        self._cards.clear()

    def _make_card(self, entry, installed_ids: set[int]) -> ModCard:
        """Create a single ModCard and bind scroll events to it."""
        url = f"https://www.nexusmods.com/{entry.domain_name}/mods/{entry.mod_id}"
        installed = entry.mod_id in installed_ids
        card = ModCard(
            self._inner, entry,
            on_view=lambda u=url: open_url(u),
            on_install=lambda e=entry: self._install_fn(e),
            on_right_click=lambda event, e=entry, u=url: self._show_context_menu(event, e, u),
            is_installed=installed,
        )
        self._bind_scroll(card.card)
        return card

    def _build_cards(self):
        """Replace all cards with the current entries."""
        self._clear_cards()
        installed_ids = self._get_installed_mod_ids()
        for entry in self._entries:
            self._cards.append(self._make_card(entry, installed_ids))
        self._regrid_cards()
        self._load_images_then_hide_loader()

    def _regrid_cards(self):
        """Place cards in a grid, recomputing column count from canvas width."""
        col_gap = 6
        slot_w = scaled(CARD_W) + col_gap * 2
        canvas_w = self._canvas.winfo_width() or (self._cols * slot_w)
        self._cols = max(1, canvas_w // slot_w)

        content_w = self._cols * slot_w
        self._canvas.itemconfig(self._inner_id, width=content_w)
        x_off = max(0, (canvas_w - content_w) // 2)
        self._canvas.coords(self._inner_id, x_off, 0)

        _pad = col_gap // 2
        for idx, mc in enumerate(self._cards):
            col = idx % self._cols
            row = idx // self._cols
            mc.card.grid(
                row=row, column=col,
                padx=(_pad, _pad),
                pady=CARD_PAD,
                sticky="n",
            )

        for c in range(self._cols):
            self._inner.grid_columnconfigure(c, weight=0, minsize=slot_w)

    def _load_images(self):
        """Kick off async image loads for cards."""
        for mc in self._cards:
            mc.load_image_async(
                getattr(mc._entry, "picture_url", "") or "",
                self._img_cache,
                self._img_loading,
                self._parent,
            )

    def _load_images_then_hide_loader(self):
        """Kick off async image loads; hide the loader once all images are done."""
        cards = list(self._cards)
        if not cards:
            self._hide_loader()
            return

        # Count cards that need an async fetch (not cached, has a URL).
        # Cards with no URL or already cached will apply synchronously and don't need counting.
        pending_urls = set()
        pending_count = 0
        for mc in cards:
            url = getattr(mc._entry, "picture_url", "") or ""
            if url and url not in self._img_cache and url not in pending_urls:
                pending_urls.add(url)
                pending_count += 1

        if pending_count == 0:
            self._load_images()
            self._hide_loader()
            return

        remaining = [pending_count]

        def _on_image_done():
            remaining[0] -= 1
            if remaining[0] <= 0:
                self._hide_loader()

        for mc in cards:
            url = getattr(mc._entry, "picture_url", "") or ""
            needs_fetch = url and url not in self._img_cache and url in pending_urls
            mc.load_image_async(
                url,
                self._img_cache,
                self._img_loading,
                self._parent,
                on_done=_on_image_done if needs_fetch else None,
            )
            # Each unique pending URL only counts once — remove after first assignment.
            if needs_fetch:
                pending_urls.discard(url)

    # ------------------------------------------------------------------
    # Loader overlay
    # ------------------------------------------------------------------

    def _show_loader(self):
        if self._loader is None:
            self._loader = CTkLoader(self._canvas_frame)

    def _hide_loader(self):
        if self._loader is not None:
            try:
                self._loader.stop_loader()
            except Exception:
                pass
            self._loader = None

    # ------------------------------------------------------------------
    # Right-click context menu (must be overridden)
    # ------------------------------------------------------------------

    def _show_context_menu(self, event, entry, url: str):
        """Override in subclass to show a context menu for the given entry."""
        raise NotImplementedError

    def _get_or_create_context_menu(self) -> CTkPopupMenu:
        """Return a shared CTkPopupMenu, creating it on first call."""
        if self._context_menu is None:
            self._context_menu = CTkPopupMenu(
                self._parent.winfo_toplevel(), width=200, title=""
            )
        return self._context_menu

    # ------------------------------------------------------------------
    # Refresh (must be overridden)
    # ------------------------------------------------------------------

    def refresh(self):
        """Override in subclass to fetch data and rebuild cards."""
        raise NotImplementedError

    # ------------------------------------------------------------------
    # Categories side panel (used by Browse and Trending)
    # ------------------------------------------------------------------

    def _build_cat_side_panel(self, tab, rowspan: int = 2):
        """Build the categories side panel (column 0, initially hidden)."""
        panel = ctk.CTkFrame(tab, fg_color=BG_PANEL, corner_radius=0, width=320)
        panel.grid(row=0, column=0, rowspan=rowspan, sticky="nsew")
        panel.grid_propagate(False)
        panel.grid_remove()
        self._cat_side_panel = panel

        # Header
        header = tk.Frame(panel, bg=BG_HEADER, height=36)
        header.pack(fill="x", side="top")
        header.pack_propagate(False)

        tk.Label(
            header, text="Categories", bg=BG_HEADER, fg=TEXT_MAIN,
            font=FONT_HEADER, anchor="w",
        ).pack(side="left", padx=10, pady=6)

        self._cat_panel_status = tk.Label(
            header, text="", bg=BG_HEADER, fg=TEXT_DIM,
            font=FONT_SMALL, anchor="e",
        )
        self._cat_panel_status.pack(side="right", padx=4)

        close_btn = tk.Label(
            header, text="×", bg=BG_HEADER, fg=TEXT_DIM,
            font=(FONT_FAMILY, 16, "bold"), cursor="hand2",
        )
        close_btn.pack(side="right", padx=4)
        close_btn.bind("<Button-1>", lambda _e: self._close_cat_sidebar())
        close_btn.bind("<Enter>", lambda _e: close_btn.configure(fg=TEXT_MAIN))
        close_btn.bind("<Leave>", lambda _e: close_btn.configure(fg=TEXT_DIM))

        tk.Frame(panel, bg=BORDER, height=1).pack(fill="x")

        # Scrollable checkbox area (populated when we have categories)
        self._cat_scroll = ctk.CTkScrollableFrame(
            panel, fg_color="transparent", corner_radius=0,
        )
        self._cat_scroll.pack(fill="both", expand=True, padx=8, pady=6)

        # Button row: Select All, Clear All
        btn_row = tk.Frame(panel, bg=BG_PANEL)
        btn_row.pack(fill="x", padx=8, pady=4)

        def _select_all_cats():
            for var in self._cat_check_vars.values():
                var.set(True)
            self._update_cat_panel_status()
            self._apply_cat_filters()

        def _clear_all_cats():
            for var in self._cat_check_vars.values():
                var.set(False)
            self._update_cat_panel_status()
            self._apply_cat_filters()

        tk.Button(
            btn_row, text="Select All",
            bg="#2d7a2d", fg="#ffffff", activebackground="#3a9e3a",
            activeforeground="#ffffff",
            relief="flat", font=FONT_SMALL, bd=0, highlightthickness=0, cursor="hand2",
            command=_select_all_cats,
        ).pack(side="left", padx=(0, 4))
        tk.Button(
            btn_row, text="Clear All",
            bg=BG_HEADER, fg=TEXT_MAIN, activebackground=BG_HOVER,
            activeforeground="#ffffff",
            relief="flat", font=FONT_SMALL, bd=0, highlightthickness=0, cursor="hand2",
            command=_clear_all_cats,
        ).pack(side="left")

        self._cat_check_vars: dict[str, tk.BooleanVar] = {}
        self._selected_cat_names: list[str] = []
        self._categories_cache: dict[str, list] = {}
        self._active_game_domain: str = ""
        self._cat_panel_open: bool = False
        self._cat_cb_pool: list[ctk.CTkCheckBox] = []
        self._cat_cb_pool_used: int = 0

        # Route wheel events anywhere over the panel (empty areas, header,
        # button row, scroll canvas itself) into the category scroll canvas.
        for w in (panel, header, btn_row,
                  self._cat_scroll, self._cat_scroll._parent_canvas):
            self._bind_cat_wheel_single(w)

    def _populate_cat_sidebar(self, categories: list):
        """Reconfigure pooled checkboxes for the category list."""
        self._cat_check_vars.clear()

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

        # Build flat ordered list of (name, is_child) tuples
        flat: list[tuple[str, bool]] = []
        for p in parents:
            flat.append((p.name, False))
            for ch in children_map.get(p.category_id, []):
                flat.append((ch.name, True))

        def _on_change(*_):
            self._update_cat_panel_status()
            self._apply_cat_filters()

        # Grow pool if needed
        while len(self._cat_cb_pool) < len(flat):
            cb = ctk.CTkCheckBox(
                self._cat_scroll,
                text="",
                variable=tk.BooleanVar(),
                font=FONT_NORMAL,
                text_color=TEXT_MAIN,
                fg_color=ACCENT,
                hover_color=ACCENT_HOV,
                border_color=BORDER,
                checkmark_color="white",
                command=_on_change,
            )
            self._bind_cat_wheel_single(cb)
            self._cat_cb_pool.append(cb)

        # Hide previously used slots beyond new count
        for i in range(len(flat), self._cat_cb_pool_used):
            self._cat_cb_pool[i].pack_forget()

        # Reconfigure visible slots
        for i, (name, is_child) in enumerate(flat):
            var = tk.BooleanVar(value=name in self._selected_cat_names)
            self._cat_check_vars[name] = var
            cb = self._cat_cb_pool[i]
            cb.configure(
                text=name,
                variable=var,
                font=FONT_SMALL if is_child else FONT_NORMAL,
                text_color=TEXT_DIM if is_child else TEXT_MAIN,
                command=_on_change,
            )
            cb.pack(anchor="w", padx=16 if is_child else 0, pady=2)

        self._cat_cb_pool_used = len(flat)
        self._update_cat_panel_status()

    def _bind_cat_wheel_single(self, widget):
        """Bind scroll wheel to a single widget and its direct children."""
        def _scroll(e):
            try:
                canv = self._cat_scroll._parent_canvas
                if getattr(e, "num", None) == 4 or (getattr(e, "delta", 0) or 0) > 0:
                    canv.yview_scroll(-3, "units")
                else:
                    canv.yview_scroll(3, "units")
            except Exception:
                pass
            return "break"
        # On Tk >= 8.7 CTkScrollableFrame (self._cat_scroll) handles <MouseWheel>
        # via its own bind_all; only supplement Button-4/5 for Tk 8.6.
        if not LEGACY_WHEEL_REDUNDANT:
            widget.bind("<Button-4>", _scroll, add="+")
            widget.bind("<Button-5>", _scroll, add="+")
            for child in widget.winfo_children():
                child.bind("<Button-4>", _scroll, add="+")
                child.bind("<Button-5>", _scroll, add="+")

    def _update_cat_panel_status(self):
        n = len([v for v in self._cat_check_vars.values() if v.get()])
        if n:
            self._cat_panel_status.configure(text=f"{n} selected", fg=ACCENT)
        else:
            self._cat_panel_status.configure(text="all categories", fg=TEXT_DIM)

    def _apply_cat_filters(self):
        """Apply selected categories and refresh."""
        self._selected_cat_names = [
            name for name, v in self._cat_check_vars.items() if v.get()
        ]
        self._cat_filter_btn.configure(text_color="white", fg_color="#2d7a2d")
        self.refresh()

    def _sync_game_domain(self, domain: str) -> None:
        if not domain or domain == self._active_game_domain:
            return
        self._active_game_domain = domain
        self._selected_cat_names = []
        self._cat_filter_btn.configure(fg_color="#2d7a2d", text_color="white")

    def _toggle_cat_sidebar(self, log_prefix: str = ""):
        """Toggle the categories side panel (load categories if needed)."""
        api = self._get_api()
        if api is None:
            self._log(f"{log_prefix}Login to Nexus first")
            return
        domain = self._get_game_domain()
        if not domain:
            self._log(f"{log_prefix}No game selected.")
            return
        self._sync_game_domain(domain)

        if self._cat_panel_open:
            self._close_cat_sidebar()
            return

        if domain in self._categories_cache:
            self._open_cat_sidebar(domain, self._categories_cache[domain])
            return

        self._cat_filter_btn.configure(state="disabled", text="Loading…")

        def _worker():
            try:
                cats = api.get_game_categories(domain)
                self._categories_cache[domain] = cats

                def _done():
                    self._cat_filter_btn.configure(state="normal", text="Categories")
                    self._open_cat_sidebar(domain, cats)

                self._parent.after(0, _done)
            except Exception as exc:
                def _err(exc=exc):
                    self._cat_filter_btn.configure(state="normal", text="Categories")
                    self._log(f"{log_prefix}Failed to load categories — {exc}")
                self._parent.after(0, _err)

        threading.Thread(target=_worker, daemon=True).start()

    def _open_cat_sidebar(self, domain: str, categories: list):
        """Show the categories side panel and populate it."""
        self._cat_panel_open = True
        self._parent.grid_columnconfigure(0, minsize=320)
        self._cat_side_panel.grid()
        self._populate_cat_sidebar(categories)
        self._cat_filter_btn.configure(fg_color=ACCENT, hover_color=ACCENT_HOV)

    def _close_cat_sidebar(self):
        """Hide the categories side panel."""
        self._cat_panel_open = False
        self._cat_side_panel.grid_remove()
        self._parent.grid_columnconfigure(0, minsize=0)
        self._cat_filter_btn.configure(fg_color="#2d7a2d", hover_color="#3a9e3a")
