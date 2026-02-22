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

import threading
import tkinter as tk
import webbrowser
from dataclasses import dataclass
from typing import Callable, Optional

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


@dataclass
class BrowseModEntry:
    """A mod entry from the browse endpoints."""
    mod_id: int = 0
    domain_name: str = ""
    name: str = ""
    author: str = ""
    version: str = ""
    summary: str = ""
    endorsement_count: int = 0


CATEGORIES = [
    ("Trending",        "get_trending"),
    ("Latest Added",    "get_latest_added"),
    ("Latest Updated",  "get_latest_updated"),
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
    ):
        self._parent = parent_tab
        self._log = log_fn or (lambda msg: None)
        self._get_api = get_api or (lambda: None)
        self._get_game_domain = get_game_domain or (lambda: "")
        self._install_fn = install_fn or (lambda entry: None)

        self._entries: list[BrowseModEntry] = []
        self._hover_idx: int = -1
        self._canvas_w: int = 400
        self._view_btns: list[tk.Button] = []
        self._install_btns: list[tk.Button] = []
        self._loading: bool = False
        self._cat_idx: int = 0  # index into CATEGORIES

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

        self._cat_btn = tk.Button(
            toolbar, text=f"▸ {CATEGORIES[0][0]}",
            bg=BG_HEADER, fg=TEXT_MAIN, activebackground=BG_HOVER,
            relief="flat", font=FONT_SMALL,
            bd=0, cursor="hand2",
            command=self._cycle_category,
        )
        self._cat_btn.pack(side="left", padx=8, pady=2)

        self._refresh_btn = tk.Button(
            toolbar, text="↺ Refresh",
            bg=BG_HEADER, fg=TEXT_MAIN, activebackground=BG_HOVER,
            relief="flat", font=FONT_SMALL,
            bd=0, cursor="hand2",
            command=self.refresh,
        )
        self._refresh_btn.pack(side="left", padx=4, pady=2)

        self._status_label = tk.Label(
            toolbar, text="Click Refresh to browse mods", anchor="w",
            font=FONT_SMALL, fg=TEXT_DIM, bg=BG_HEADER,
        )
        self._status_label.pack(side="left", padx=4)

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

    # ------------------------------------------------------------------
    # Category cycling
    # ------------------------------------------------------------------

    def _cycle_category(self):
        """Cycle to the next browse category and auto-refresh."""
        self._cat_idx = (self._cat_idx + 1) % len(CATEGORIES)
        label, _ = CATEGORIES[self._cat_idx]
        self._cat_btn.configure(text=f"▸ {label}")
        self.refresh()

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

        if self._loading:
            return
        self._loading = True
        self._refresh_btn.configure(state="disabled")
        cat_label, cat_method = CATEGORIES[self._cat_idx]
        self._status_label.configure(text=f"Loading {cat_label}…")

        def _worker():
            try:
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
                        endorsement_count=get("endorsement_count", 0),
                    ))

                def _done():
                    self._entries = entries
                    self._loading = False
                    self._refresh_btn.configure(state="normal")
                    self._status_label.configure(
                        text=f"{len(entries)} {cat_label.lower()} mod(s) for {domain}"
                    )
                    self._rebuild_buttons()
                    self._repaint()
                    self._log(f"Browse: Loaded {len(entries)} {cat_label.lower()} mod(s) for {domain}.")

                self._parent.after(0, _done)

            except Exception as exc:
                def _err():
                    self._loading = False
                    self._refresh_btn.configure(state="normal")
                    self._status_label.configure(text="Error")
                    self._log(f"Browse: Failed — {exc}")
                self._parent.after(0, _err)

        threading.Thread(target=_worker, daemon=True).start()

    # ------------------------------------------------------------------
    # Button management
    # ------------------------------------------------------------------

    def _rebuild_buttons(self):
        """Destroy old buttons and create View + Install per entry."""
        for btn in self._view_btns:
            btn.destroy()
        for btn in self._install_btns:
            btn.destroy()
        self._view_btns.clear()
        self._install_btns.clear()

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
                cursor="hand2",
                command=lambda e=entry: self._install_fn(e),
            )
            self._view_btns.append(view_btn)
            self._install_btns.append(install_btn)

        self._place_buttons()

    def _place_buttons(self):
        """Move every button to the correct canvas-coordinate position."""
        cw = self._canvas_w
        install_cx = cw - INSTALL_W // 2 - 4
        view_cx = install_cx - INSTALL_W // 2 - BTN_GAP - VIEW_W // 2

        for row in range(len(self._view_btns)):
            y_center = row * ROW_H + ROW_H // 2
            self._canvas.create_window(
                view_cx, y_center,
                window=self._view_btns[row],
                width=VIEW_W,
                height=ROW_H - 14,
                tags=f"vbtn{row}",
            )
            self._canvas.create_window(
                install_cx, y_center,
                window=self._install_btns[row],
                width=INSTALL_W,
                height=ROW_H - 14,
                tags=f"ibtn{row}",
            )

    # ------------------------------------------------------------------
    # Drawing
    # ------------------------------------------------------------------

    def _repaint(self):
        """Redraw row backgrounds and text."""
        self._canvas.delete("bg")
        self._canvas.delete("txt")

        cw = self._canvas_w
        entries = self._entries
        total_h = len(entries) * ROW_H

        canvas_top = int(self._canvas.canvasy(0))
        canvas_bottom = canvas_top + self._canvas.winfo_height()

        btn_left = cw - BTN_COL_W
        name_max_px = btn_left - NAME_PAD_L - 8

        for row, entry in enumerate(entries):
            y_top = row * ROW_H
            y_bot = y_top + ROW_H

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

            # Primary text: mod name + version + endorsements
            title = entry.name
            if entry.version:
                title += f"  v{entry.version}"
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
            sub_text = " ".join(sub_parts)
            sub_text = _truncate(self._canvas, sub_text, FONT_SMALL, max(name_max_px, 20))
            if sub_text:
                self._canvas.create_text(
                    NAME_PAD_L, y_top + 34,
                    text=sub_text, anchor="w",
                    font=FONT_SMALL, fill=TEXT_DIM, tags="txt",
                )

        self._canvas.configure(scrollregion=(0, 0, cw, max(total_h, 1)))
        self._canvas.tag_raise("all")

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
        self._place_buttons()
        self._repaint()

    # ------------------------------------------------------------------
    # Hover
    # ------------------------------------------------------------------

    def _on_motion(self, event):
        y = int(self._canvas.canvasy(event.y))
        idx = y // ROW_H
        new_idx = idx if 0 <= idx < len(self._entries) else -1
        if new_idx != self._hover_idx:
            self._hover_idx = new_idx
            self._repaint()

    def _on_leave(self, _event):
        if self._hover_idx != -1:
            self._hover_idx = -1
            self._repaint()

    # ------------------------------------------------------------------
    # Right-click context menu
    # ------------------------------------------------------------------

    def _on_right_click(self, event):
        y = int(self._canvas.canvasy(event.y))
        idx = y // ROW_H
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
