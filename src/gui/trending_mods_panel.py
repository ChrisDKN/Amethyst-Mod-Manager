"""
Trending Mods panel — displays trending mods from Nexus Mods for the
currently selected game.

Trending = mods published in the last 7 days, sorted by endorsements (highest first).
Uses the Nexus GraphQL API with createdAt filter and endorsements sort.
Each mod is shown as a CTkCard with image, name, stats, summary, View/Install.
Right-click offers Open on Nexus, Install Mod, Track Mod, Endorse Mod.
"""

from __future__ import annotations

import threading
import tkinter as tk
from Utils.xdg import open_url
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
    BORDER,
    ACCENT,
    ACCENT_HOV,
    BG_HOVER,
    TEXT_MAIN,
    TEXT_DIM,
    FONT_HEADER,
    FONT_NORMAL,
    FONT_SMALL,
    scaled,
)

# Mods per page when browsing trending
PAGE_SIZE = 20


@dataclass
class TrendingModEntry:
    """A trending mod (published in last 7 days, by endorsements)."""
    mod_id: int = 0
    domain_name: str = ""
    name: str = ""
    author: str = ""
    version: str = ""
    summary: str = ""
    endorsement_count: int = 0
    downloads_total: int = 0
    picture_url: str = ""


class TrendingModsPanel:
    """
    Card-grid panel listing trending mods for the selected game.
    Trending = mods published in the last 7 days, sorted by endorsements.
    """

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

        self._entries: list[TrendingModEntry] = []
        self._cards: list[ModCard] = []
        self._loading: bool = False
        self._page: int = 0
        self._selected_cat_names: list[str] = []
        self._categories_cache: dict[str, list] = {}
        self._active_game_domain: str = ""
        self._img_cache: dict[str, ctk.CTkImage] = {}
        self._img_loading: set[str] = set()
        self._cols: int = CARD_COLS
        self._regrid_after_id = None
        self._context_menu: CTkPopupMenu | None = None
        self._cat_panel_open: bool = False

        self._build(parent_tab)

    # ------------------------------------------------------------------
    # Build UI
    # ------------------------------------------------------------------

    def _build(self, tab):
        tab.grid_rowconfigure(1, weight=1)
        tab.grid_columnconfigure(0, weight=0, minsize=0)  # categories side panel
        tab.grid_columnconfigure(1, weight=1)            # main content

        self._build_cat_side_panel(tab)

        # Toolbar
        toolbar = tk.Frame(tab, bg=BG_HEADER, height=28)
        toolbar.grid(row=0, column=1, sticky="ew")
        toolbar.grid_propagate(False)

        self._refresh_btn = ctk.CTkButton(
            toolbar, text="↺ Refresh", width=72, height=26,
            fg_color=ACCENT, hover_color=ACCENT_HOV, text_color="white",
            font=FONT_HEADER, command=self.refresh,
        )
        self._refresh_btn.pack(side="left", padx=8, pady=2)

        self._cat_filter_btn = ctk.CTkButton(
            toolbar, text="Categories", width=80, height=26,
            fg_color="#2d7a2d", hover_color="#3a9e3a", text_color="white",
            font=FONT_HEADER, command=self._toggle_cat_sidebar,
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

        self._status_label = ctk.CTkLabel(
            toolbar, text="Click Refresh to load trending mods", anchor="w",
            font=FONT_SMALL, text_color=TEXT_DIM, fg_color=BG_HEADER,
        )
        self._status_label.pack(side="left", padx=4, fill="x", expand=True)

        # Scrollable card area
        canvas_frame = tk.Frame(tab, bg=BG_DEEP, bd=0, highlightthickness=0)
        canvas_frame.grid(row=1, column=1, sticky="nsew")
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
        self._inner.bind("<Button-4>",   lambda e: self._scroll(-100))
        self._inner.bind("<Button-5>",   lambda e: self._scroll(100))
        self._inner.bind("<MouseWheel>", self._on_mousewheel)

    def _on_inner_configure(self, _event=None):
        self._canvas.configure(scrollregion=(
            0, 0, self._inner.winfo_reqwidth(), self._inner.winfo_reqheight(),
        ))

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
        self._canvas.update_idletasks()
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

    def _bind_scroll(self, widget):
        widget.bind("<Button-4>",   lambda e: self._scroll(-50), add="+")
        widget.bind("<Button-5>",   lambda e: self._scroll(50),  add="+")
        widget.bind("<MouseWheel>", self._on_mousewheel, add="+")
        for child in widget.winfo_children():
            self._bind_scroll(child)

    # ------------------------------------------------------------------
    # Categories side panel
    # ------------------------------------------------------------------

    def _build_cat_side_panel(self, tab):
        """Build the categories side panel (column 0, initially hidden)."""
        panel = ctk.CTkFrame(tab, fg_color=BG_PANEL, corner_radius=0, width=280)
        panel.grid(row=0, column=0, rowspan=2, sticky="nsew")
        panel.grid_propagate(False)
        panel.grid_remove()
        self._cat_side_panel = panel

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
            font=("Segoe UI", 16, "bold"), cursor="hand2",
        )
        close_btn.pack(side="right", padx=4)
        close_btn.bind("<Button-1>", lambda _e: self._close_cat_sidebar())
        close_btn.bind("<Enter>", lambda _e: close_btn.configure(fg=TEXT_MAIN))
        close_btn.bind("<Leave>", lambda _e: close_btn.configure(fg=TEXT_DIM))

        tk.Frame(panel, bg=BORDER, height=1).pack(fill="x")

        self._cat_scroll = ctk.CTkScrollableFrame(
            panel, fg_color="transparent", corner_radius=0,
        )
        self._cat_scroll.pack(fill="both", expand=True, padx=8, pady=6)

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

    def _populate_cat_sidebar(self, categories: list):
        for w in self._cat_scroll.winfo_children():
            w.destroy()
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

        def _on_change(*_):
            self._update_cat_panel_status()
            self._apply_cat_filters()

        for p in parents:
            var = tk.BooleanVar(value=p.name in self._selected_cat_names)
            self._cat_check_vars[p.name] = var
            cb = ctk.CTkCheckBox(
                self._cat_scroll,
                text=p.name,
                variable=var,
                font=FONT_NORMAL,
                text_color=TEXT_MAIN,
                fg_color=ACCENT,
                hover_color=ACCENT_HOV,
                border_color=BORDER,
                checkmark_color="white",
                command=_on_change,
            )
            cb.pack(anchor="w", pady=2)
            for ch in children_map.get(p.category_id, []):
                cvar = tk.BooleanVar(value=ch.name in self._selected_cat_names)
                self._cat_check_vars[ch.name] = cvar
                ccb = ctk.CTkCheckBox(
                    self._cat_scroll,
                    text=ch.name,
                    variable=cvar,
                    font=FONT_SMALL,
                    text_color=TEXT_DIM,
                    fg_color=ACCENT,
                    hover_color=ACCENT_HOV,
                    border_color=BORDER,
                    checkmark_color="white",
                    command=_on_change,
                )
                ccb.pack(anchor="w", padx=16, pady=2)

        self._update_cat_panel_status()
        self._bind_cat_wheel_recursive(self._cat_scroll)

    def _bind_cat_wheel_recursive(self, widget):
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
        widget.bind("<MouseWheel>", _scroll, add="+")
        widget.bind("<Button-4>", _scroll, add="+")
        widget.bind("<Button-5>", _scroll, add="+")
        for child in widget.winfo_children():
            self._bind_cat_wheel_recursive(child)

    def _update_cat_panel_status(self):
        n = len([v for v in self._cat_check_vars.values() if v.get()])
        if n:
            self._cat_panel_status.configure(text=f"{n} selected", fg=ACCENT)
        else:
            self._cat_panel_status.configure(text="all categories", fg=TEXT_DIM)

    def _apply_cat_filters(self):
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

    def _toggle_cat_sidebar(self):
        api = self._get_api()
        if api is None:
            self._log("Trending: Set your Nexus API key first.")
            return
        domain = self._get_game_domain()
        if not domain:
            self._log("Trending: No game selected.")
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
                    self._log(f"Trending: Failed to load categories — {exc}")
                self._parent.after(0, _err)

        threading.Thread(target=_worker, daemon=True).start()

    def _open_cat_sidebar(self, domain: str, categories: list):
        self._cat_panel_open = True
        self._parent.grid_columnconfigure(0, minsize=280)
        self._cat_side_panel.grid()
        self._populate_cat_sidebar(categories)
        self._cat_filter_btn.configure(fg_color=ACCENT, hover_color=ACCENT_HOV)

    def _close_cat_sidebar(self):
        self._cat_panel_open = False
        self._cat_side_panel.grid_remove()
        self._parent.grid_columnconfigure(0, minsize=0)
        self._cat_filter_btn.configure(fg_color="#2d7a2d", hover_color="#3a9e3a")

    def _go_prev_page(self):
        if self._page > 0 and not self._loading:
            self._page -= 1
            self._load_page()

    def _go_next_page(self):
        if not self._loading and len(self._entries) >= PAGE_SIZE:
            self._page += 1
            self._load_page()

    # ------------------------------------------------------------------
    # Card rendering
    # ------------------------------------------------------------------

    def _clear_cards(self):
        for c in self._cards:
            c.card.destroy()
        self._cards.clear()

    def _make_card(self, entry: TrendingModEntry, installed_ids: set[int]) -> ModCard:
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
        self._clear_cards()
        installed_ids = self._get_installed_mod_ids()
        for entry in self._entries:
            self._cards.append(self._make_card(entry, installed_ids))
        self._regrid_cards()
        self._load_images()

    def _regrid_cards(self):
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
        for mc in self._cards:
            mc.load_image_async(
                getattr(mc._entry, "picture_url", "") or "",
                self._img_cache,
                self._img_loading,
                self._parent,
            )

    # ------------------------------------------------------------------
    # Refresh / Pagination
    # ------------------------------------------------------------------

    def refresh(self):
        """Fetch page 0 of trending mods."""
        self._page = 0
        self._load_page()

    def _load_page(self):
        """Fetch the current page of trending mods."""
        api = self._get_api()
        if api is None:
            self._log("Trending: Set your Nexus API key first.")
            return
        domain = self._get_game_domain()
        if not domain:
            self._log("Trending: No game selected.")
            return
        self._sync_game_domain(domain)

        if self._loading:
            return
        self._loading = True
        self._refresh_btn.configure(state="disabled")
        self._prev_btn.configure(state="disabled")
        self._next_btn.configure(state="disabled")
        page = self._page
        self._status_label.configure(
            text=f"Loading trending (page {page + 1})…"
        )

        def _worker():
            try:
                mod_infos = api.get_trending_mods_graphql(
                    domain,
                    count=PAGE_SIZE,
                    offset=page * PAGE_SIZE,
                    category_names=self._selected_cat_names or None,
                )
                entries: list[TrendingModEntry] = []
                for info in mod_infos:
                    entries.append(TrendingModEntry(
                        mod_id=info.mod_id,
                        domain_name=getattr(info, "domain_name", domain),
                        name=info.name or f"Mod {info.mod_id}",
                        author=info.author or "",
                        version=info.version or "",
                        summary=info.summary or "",
                        endorsement_count=info.endorsement_count or 0,
                        downloads_total=info.downloads_total or 0,
                        picture_url=info.picture_url or "",
                    ))

                def _done():
                    self._entries = entries
                    self._loading = False
                    self._refresh_btn.configure(state="normal")
                    self._prev_btn.configure(
                        state="normal" if page > 0 else "disabled"
                    )
                    self._next_btn.configure(
                        state="normal" if len(entries) >= PAGE_SIZE else "disabled"
                    )
                    self._build_cards()
                    self._canvas.yview_moveto(0)
                    self._status_label.configure(
                        text=f"Trending: page {page + 1}"
                    )
                    self._log(
                        f"Trending: Loaded page {page + 1} — {len(entries)} mod(s)"
                    )

                self._parent.after(0, _done)

            except Exception as exc:
                def _err():
                    self._loading = False
                    self._refresh_btn.configure(state="normal")
                    self._prev_btn.configure(state="normal")
                    self._next_btn.configure(state="normal")
                    self._status_label.configure(text="Error")
                    self._log(f"Trending: Failed — {exc}")
                self._parent.after(0, _err)

        threading.Thread(target=_worker, daemon=True).start()

    # ------------------------------------------------------------------
    # Right-click context menu
    # ------------------------------------------------------------------

    def _show_context_menu(self, event, entry: TrendingModEntry, url: str):
        if self._context_menu is None:
            self._context_menu = CTkPopupMenu(
                self._parent.winfo_toplevel(), width=200, title=""
            )
        menu = self._context_menu
        menu.clear()
        menu.add_command("Open on Nexus", lambda: open_url(url))
        menu.add_command("Install Mod", lambda: self._install_fn(entry))
        menu.add_separator()
        menu.add_command("Track Mod", lambda: self._track_mod(entry))
        menu.add_command("Endorse Mod", lambda: self._endorse_mod(entry))
        menu.popup(event.x_root, event.y_root)

    def _track_mod(self, entry: TrendingModEntry):
        api = self._get_api()
        if api is None:
            self._log("Trending: No API key set.")
            return

        def _worker():
            try:
                api.track_mod(entry.domain_name, entry.mod_id)
                self._parent.after(0,
                    lambda: self._log(f"Trending: Now tracking '{entry.name}' ({entry.mod_id})."))
            except Exception as exc:
                self._parent.after(0,
                    lambda: self._log(f"Trending: Track failed — {exc}"))

        threading.Thread(target=_worker, daemon=True).start()

    def _endorse_mod(self, entry: TrendingModEntry):
        api = self._get_api()
        if api is None:
            self._log("Trending: No API key set.")
            return

        def _worker():
            try:
                api.endorse_mod(entry.domain_name, entry.mod_id, entry.version)
                self._parent.after(0,
                    lambda: self._log(f"Trending: Endorsed '{entry.name}' ({entry.mod_id})."))
            except Exception as exc:
                self._parent.after(0,
                    lambda: self._log(f"Trending: Endorse failed — {exc}"))

        threading.Thread(target=_worker, daemon=True).start()
