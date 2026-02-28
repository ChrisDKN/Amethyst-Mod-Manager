"""
Endorsed Mods panel — displays mods the user has endorsed on Nexus Mods
for the currently selected game.

Fetches the endorsement list via the Nexus v1 REST API, then enriches
each entry with full mod details (name, author, version, summary, picture)
by calling ``get_mod()`` per mod.  Results are cached so switching back to
the tab is instant until the user clicks **Refresh**.

Each mod is shown as a CTkCard with image, name, stats, summary, View/Install.
Right-click offers **Open on Nexus**, **Install Mod**, and **Abstain** (un-endorse).
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
    ACCENT,
    ACCENT_HOV,
    TEXT_MAIN,
    TEXT_DIM,
    FONT_SMALL,
)


@dataclass
class EndorsedModEntry:
    """An endorsed mod with enriched info."""
    mod_id: int = 0
    domain_name: str = ""
    name: str = ""
    author: str = ""
    version: str = ""
    summary: str = ""
    endorsement_count: int = 0
    downloads_total: int = 0
    picture_url: str = ""
    endorsed_date: int = 0
    endorsed_status: str = "Endorsed"


class EndorsedModsPanel:
    """
    Card-grid panel listing mods endorsed by the user on Nexus Mods.

    Built into an existing parent widget (a tab frame from CTkTabview).
    """

    def __init__(
        self,
        parent_tab: tk.Widget,
        log_fn: Optional[Callable] = None,
        get_api: Optional[Callable] = None,
        get_game_domain: Optional[Callable] = None,
        install_fn: Optional[Callable] = None,
    ):
        self._parent = parent_tab
        self._log = log_fn or (lambda msg: None)
        self._get_api = get_api or (lambda: None)
        self._get_game_domain = get_game_domain or (lambda: "")
        self._install_fn = install_fn or (lambda entry: None)

        self._entries: list[EndorsedModEntry] = []
        self._cards: list[ModCard] = []
        self._loading: bool = False
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
        tab.grid_columnconfigure(0, weight=1)

        # Toolbar
        toolbar = tk.Frame(tab, bg=BG_HEADER, height=28)
        toolbar.grid(row=0, column=0, sticky="ew")
        toolbar.grid_propagate(False)

        self._refresh_btn = tk.Button(
            toolbar, text="↺ Refresh",
            bg=ACCENT, fg="#ffffff", activebackground=ACCENT_HOV,
            activeforeground="#ffffff",
            relief="flat", font=FONT_SMALL,
            bd=0, highlightthickness=0, cursor="hand2",
            command=self.refresh,
        )
        self._refresh_btn.pack(side="left", padx=8, pady=2)

        self._status_label = tk.Label(
            toolbar, text="Click Refresh to load endorsed mods", anchor="w",
            font=FONT_SMALL, fg=TEXT_DIM, bg=BG_HEADER,
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
        self._canvas.bind("<Button-4>",   lambda e: self._scroll(-100))
        self._canvas.bind("<Button-5>",   lambda e: self._scroll(100))
        self._canvas.bind("<MouseWheel>", self._on_mousewheel)
        self._inner.bind("<Button-4>",   lambda e: self._scroll(-100))
        self._inner.bind("<Button-5>",   lambda e: self._scroll(100))
        self._inner.bind("<MouseWheel>", self._on_mousewheel)

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

    def _make_card(self, entry: EndorsedModEntry) -> ModCard:
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
        self._clear_cards()
        for entry in self._entries:
            self._cards.append(self._make_card(entry))
        self._regrid_cards()
        self._load_images()

    def _regrid_cards(self):
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
        for c in range(self._cols):
            self._inner.grid_columnconfigure(c, weight=1)

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
        """Fetch endorsed mods from the Nexus API."""
        api = self._get_api()
        if api is None:
            self._log("Endorsed Mods: Set your Nexus API key first.")
            return
        domain = self._get_game_domain()
        if not domain:
            self._log("Endorsed Mods: No game selected.")
            return

        if self._loading:
            return
        self._loading = True
        self._refresh_btn.configure(state="disabled")
        self._status_label.configure(text="Loading…")

        def _worker():
            try:
                all_endorsements = api.get_endorsements()
                game_endorsed = [
                    e for e in all_endorsements
                    if e.get("domain_name", "") == domain
                    and e.get("status", "") == "Endorsed"
                ]
                entries: list[EndorsedModEntry] = []
                total = len(game_endorsed)

                for i, e in enumerate(game_endorsed):
                    mod_id = e.get("mod_id", 0)
                    if mod_id <= 0:
                        continue
                    entry = EndorsedModEntry(
                        mod_id=mod_id,
                        domain_name=domain,
                        endorsed_date=e.get("date", 0),
                        endorsed_status=e.get("status", "Endorsed"),
                    )
                    try:
                        info = api.get_mod(domain, mod_id)
                        entry.name = getattr(info, "name", "") or f"Mod {mod_id}"
                        entry.author = getattr(info, "author", "")
                        entry.version = getattr(info, "version", "")
                        entry.summary = getattr(info, "summary", "")
                        entry.endorsement_count = getattr(info, "endorsement_count", 0)
                        entry.downloads_total = getattr(info, "downloads_total", 0)
                        entry.picture_url = getattr(info, "picture_url", "") or ""
                    except Exception:
                        entry.name = f"Mod {mod_id}"
                    entries.append(entry)
                    if (i + 1) % 5 == 0 or (i + 1) == total:
                        self._parent.after(0, lambda n=i+1, t=total:
                            self._status_label.configure(text=f"Loading… {n}/{t}"))

                def _done():
                    self._entries = entries
                    self._loading = False
                    self._refresh_btn.configure(state="normal")
                    self._status_label.configure(
                        text=f"{len(entries)} endorsed mod(s) for {domain}"
                    )
                    self._build_cards()
                    self._canvas.yview_moveto(0)
                    self._log(f"Endorsed Mods: Found {len(entries)} endorsed mod(s) for {domain}.")

                self._parent.after(0, _done)

            except Exception as exc:
                def _err():
                    self._loading = False
                    self._refresh_btn.configure(state="normal")
                    self._status_label.configure(text="Error")
                    self._log(f"Endorsed Mods: Failed — {exc}")
                self._parent.after(0, _err)

        threading.Thread(target=_worker, daemon=True).start()

    # ------------------------------------------------------------------
    # Right-click context menu
    # ------------------------------------------------------------------

    def _show_context_menu(self, event, entry: EndorsedModEntry, url: str):
        if self._context_menu is None:
            self._context_menu = CTkPopupMenu(
                self._parent.winfo_toplevel(), width=200, title=""
            )
        menu = self._context_menu
        menu.clear()
        menu.add_command("Open on Nexus", lambda: webbrowser.open(url))
        menu.add_command("Install Mod", lambda: self._install_fn(entry))
        menu.add_separator()
        menu.add_command("Abstain from Endorsement", lambda: self._abstain_mod(entry))
        menu.popup(event.x_root, event.y_root)

    def _abstain_mod(self, entry: EndorsedModEntry):
        api = self._get_api()
        if api is None:
            self._log("Endorsed Mods: No API key set.")
            return

        def _worker():
            try:
                api.abstain_mod(entry.domain_name, entry.mod_id, entry.version)
                def _done():
                    self._log(f"Endorsed Mods: Abstained from '{entry.name}' ({entry.mod_id}).")
                    self._entries = [e for e in self._entries if e.mod_id != entry.mod_id]
                    self._build_cards()
                    self._status_label.configure(
                        text=f"{len(self._entries)} endorsed mod(s) for {entry.domain_name}"
                    )
                self._parent.after(0, _done)
            except Exception as exc:
                self._parent.after(0,
                    lambda: self._log(f"Endorsed Mods: Abstain failed — {exc}"))

        threading.Thread(target=_worker, daemon=True).start()
