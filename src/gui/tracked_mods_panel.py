"""
Tracked Mods panel — displays mods the user is tracking on Nexus Mods
for the currently selected game.

Fetches the tracked-mod list via the Nexus v1 REST API, then enriches
each entry with full mod details (name, author, version, summary, picture)
by calling ``get_mod()`` per mod.  Results are cached so switching back to
the tab is instant until the user clicks **Refresh**.

Each mod is shown as a CTkCard with image, name, stats, summary, View/Install.
Right-click offers **Open on Nexus**, **Install Mod**, and **Untrack Mod**.
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
    ACCENT,
    ACCENT_HOV,
    TEXT_DIM,
    FONT_HEADER,
    FONT_SMALL,
    scaled,
)


@dataclass
class TrackedModEntry:
    """A tracked mod with enriched info."""
    mod_id: int = 0
    domain_name: str = ""
    name: str = ""
    author: str = ""
    version: str = ""
    summary: str = ""
    endorsement_count: int = 0
    downloads_total: int = 0
    picture_url: str = ""


class TrackedModsPanel:
    """
    Card-grid panel listing mods tracked by the user on Nexus Mods.

    Built into an existing parent widget (a tab frame from CTkTabview).
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

        self._entries: list[TrackedModEntry] = []
        self._cards: list[ModCard] = []
        self._loading: bool = False
        self._img_cache: dict[str, ctk.CTkImage] = {}
        self._img_loading: set[str] = set()
        self._cols: int = CARD_COLS
        self._context_menu: CTkPopupMenu | None = None
        self._regrid_after_id = None

        self._build(parent_tab)

    # ------------------------------------------------------------------
    # Build UI
    # ------------------------------------------------------------------

    def _build(self, tab):
        tab.grid_rowconfigure(1, weight=1)
        tab.grid_columnconfigure(0, weight=1)

        # Toolbar
        toolbar = tk.Frame(tab, bg=BG_HEADER, height=28)
        toolbar.grid(row=0, column=0, sticky="ew")
        toolbar.grid_propagate(False)

        self._refresh_btn = ctk.CTkButton(
            toolbar, text="↺ Refresh", width=72, height=26,
            fg_color=ACCENT, hover_color=ACCENT_HOV, text_color="white",
            font=FONT_HEADER, command=self.refresh,
        )
        self._refresh_btn.pack(side="left", padx=8, pady=2)

        self._status_label = ctk.CTkLabel(
            toolbar, text="Click Refresh to load tracked mods", anchor="w",
            font=FONT_SMALL, text_color=TEXT_DIM, fg_color=BG_HEADER,
        )
        self._status_label.pack(side="left", padx=4, fill="x", expand=True)

        # Scrollable card area
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
    # Card rendering
    # ------------------------------------------------------------------

    def _clear_cards(self):
        for c in self._cards:
            c.card.destroy()
        self._cards.clear()

    def _make_card(self, entry: TrackedModEntry, installed_ids: set[int]) -> ModCard:
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
    # Refresh
    # ------------------------------------------------------------------

    def refresh(self):
        """Fetch tracked mods from the Nexus API."""
        api = self._get_api()
        if api is None:
            self._log("Tracked Mods: Set your Nexus API key first.")
            return
        domain = self._get_game_domain()
        if not domain:
            self._log("Tracked Mods: No game selected.")
            return

        if self._loading:
            return
        self._loading = True
        self._refresh_btn.configure(state="disabled")
        self._status_label.configure(text="Loading…")

        def _worker():
            try:
                all_tracked = api.get_tracked_mods()
                game_tracked = [t for t in all_tracked if t.get("domain_name", "") == domain]
                total = len(game_tracked)

                # Fetch all mod info in batches of 20 via GraphQL (no rate limit cost)
                mod_ids = [t.get("mod_id", 0) for t in game_tracked if t.get("mod_id", 0) > 0]
                self._parent.after(0, lambda n=total: self._status_label.configure(
                    text=f"Fetching info for {n} mods…"
                ))
                info_map = api.graphql_mod_info_batch([(domain, mid) for mid in mod_ids])

                entries: list[TrackedModEntry] = []
                for t in game_tracked:
                    mod_id = t.get("mod_id", 0)
                    if mod_id <= 0:
                        continue
                    info = info_map.get(mod_id)
                    entry = TrackedModEntry(mod_id=mod_id, domain_name=domain)
                    if info:
                        entry.name             = info.name or f"Mod {mod_id}"
                        entry.author           = info.author
                        entry.version          = info.version
                        entry.summary          = info.summary
                        entry.endorsement_count = info.endorsement_count
                        entry.downloads_total  = info.downloads_total
                        entry.picture_url      = info.picture_url or ""
                    else:
                        entry.name = f"Mod {mod_id}"
                    entries.append(entry)

                def _done():
                    self._entries = entries
                    self._loading = False
                    self._refresh_btn.configure(state="normal")
                    self._status_label.configure(
                        text=f"{len(entries)} tracked mod(s) for {domain}"
                    )
                    self._build_cards()
                    self._canvas.yview_moveto(0)
                    self._log(f"Tracked Mods: Found {len(entries)} tracked mod(s) for {domain}.")

                self._parent.after(0, _done)

            except Exception as exc:
                def _err():
                    self._loading = False
                    self._refresh_btn.configure(state="normal")
                    self._status_label.configure(text="Error")
                    self._log(f"Tracked Mods: Failed — {exc}")
                self._parent.after(0, _err)

        threading.Thread(target=_worker, daemon=True).start()

    # ------------------------------------------------------------------
    # Right-click context menu
    # ------------------------------------------------------------------

    def _show_context_menu(self, event, entry: TrackedModEntry, url: str):
        if self._context_menu is None:
            self._context_menu = CTkPopupMenu(
                self._parent.winfo_toplevel(), width=200, title=""
            )
        menu = self._context_menu
        menu.clear()
        menu.add_command("Open on Nexus", lambda: open_url(url))
        menu.add_command("Install Mod", lambda: self._install_fn(entry))
        menu.add_separator()
        menu.add_command("Untrack Mod", lambda: self._untrack_mod(entry))
        menu.popup(event.x_root, event.y_root)

    def _untrack_mod(self, entry: TrackedModEntry):
        api = self._get_api()
        if api is None:
            self._log("Tracked Mods: No API key set.")
            return

        def _worker():
            try:
                api.untrack_mod(entry.domain_name, entry.mod_id)
                def _done():
                    self._log(f"Tracked Mods: Untracked '{entry.name}' ({entry.mod_id}).")
                    self._entries = [e for e in self._entries if e.mod_id != entry.mod_id]
                    self._build_cards()
                    self._status_label.configure(
                        text=f"{len(self._entries)} tracked mod(s) for {entry.domain_name}"
                    )
                self._parent.after(0, _done)
            except Exception as exc:
                self._parent.after(0,
                    lambda: self._log(f"Tracked Mods: Untrack failed — {exc}"))

        threading.Thread(target=_worker, daemon=True).start()
