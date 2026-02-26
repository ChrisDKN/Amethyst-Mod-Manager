"""
Endorsed Mods panel — displays mods the user has endorsed on Nexus Mods
for the currently selected game.

Fetches the endorsement list via the Nexus v1 REST API, then enriches
each entry with full mod details (name, author, version, summary) by
calling ``get_mod()`` per mod.  Results are cached so switching back to
the tab is instant until the user clicks **Refresh**.

Each row shows the mod name, author, version, and a brief summary.
A **View** button opens the mod page on Nexus; right-click offers
**Open on Nexus**, **Install Mod**, and **Abstain** (un-endorse).
"""

from __future__ import annotations

import threading
import tkinter as tk
import webbrowser
from dataclasses import dataclass
from typing import Callable, Optional

from gui.theme import (
    BG_DEEP,
    BG_PANEL,
    BG_HEADER,
    BG_ROW,
    BG_ROW_ALT,
    BG_HOVER,
    ACCENT,
    ACCENT_HOV,
    TEXT_MAIN,
    TEXT_DIM,
    FONT_NORMAL,
    FONT_SMALL,
)

ROW_H      = 48   # taller rows to fit two lines of text
BTN_COL_W  = 150  # px reserved on the right for View + Install buttons
VIEW_W     = 60   # width of the View button
INSTALL_W  = 70   # width of the Install button
BTN_GAP    = 4    # gap between buttons
NAME_PAD_L = 10   # left padding for text


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
    endorsed_date: int = 0
    endorsed_status: str = "Endorsed"


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


class EndorsedModsPanel:
    """
    Canvas-based panel listing mods endorsed by the user on Nexus Mods.

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
        """
        Parameters
        ----------
        parent_tab : tk.Widget
            The tab frame to build into.
        log_fn : callable
            ``log_fn(msg)`` — write a message to the bottom log panel.
        get_api : callable
            ``get_api()`` → ``NexusAPI | None`` — return the current API client.
        get_game_domain : callable
            ``get_game_domain()`` → ``str`` — return the Nexus game domain for
            the currently selected game (e.g. ``"skyrimspecialedition"``).
        install_fn : callable
            ``install_fn(entry: EndorsedModEntry)`` — called when the user clicks
            Install on an endorsed mod.  The parent is responsible for the actual
            download-and-install flow.
        """
        self._parent = parent_tab
        self._log = log_fn or (lambda msg: None)
        self._get_api = get_api or (lambda: None)
        self._get_game_domain = get_game_domain or (lambda: "")
        self._install_fn = install_fn or (lambda entry: None)

        self._entries: list[EndorsedModEntry] = []
        self._hover_idx: int = -1
        self._canvas_w: int = 400
        self._view_btns: list[tk.Button] = []
        self._install_btns: list[tk.Button] = []
        self._loading: bool = False

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
            bg=BG_HEADER, fg=TEXT_MAIN, activebackground=BG_HOVER,
            relief="flat", font=FONT_SMALL,
            bd=0, cursor="hand2",
            command=self.refresh,
        )
        self._refresh_btn.pack(side="left", padx=8, pady=2)

        self._status_label = tk.Label(
            toolbar, text="Click Refresh to load endorsed mods", anchor="w",
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
            bg="#383838", troughcolor=BG_DEEP, activebackground=ACCENT,
            highlightthickness=0, bd=0,
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
    # Refresh (fetches from Nexus API in a background thread)
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
                # Filter to the current game domain and only actually endorsed
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
                    # Enrich with full mod info
                    try:
                        info = api.get_mod(domain, mod_id)
                        entry.name = getattr(info, "name", "") or f"Mod {mod_id}"
                        entry.author = getattr(info, "author", "")
                        entry.version = getattr(info, "version", "")
                        entry.summary = getattr(info, "summary", "")
                        entry.endorsement_count = getattr(info, "endorsement_count", 0)
                    except Exception:
                        entry.name = f"Mod {mod_id}"

                    entries.append(entry)

                    # Progress update in UI every 5 mods
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
                    self._rebuild_buttons()
                    self._repaint()
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
        # Install button sits at the far right, View button to its left
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

            # Primary text: mod name + version
            title = entry.name
            if entry.version:
                title += f"  v{entry.version}"
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
        menu.add_command(label="Abstain from Endorsement",
                         command=lambda e=entry: self._abstain_mod(e))
        menu.tk_popup(event.x_root, event.y_root)

    def _abstain_mod(self, entry: EndorsedModEntry):
        """Abstain from endorsing a mod via the Nexus API (removes endorsement)."""
        api = self._get_api()
        if api is None:
            self._log("Endorsed Mods: No API key set.")
            return

        def _worker():
            try:
                api.abstain_mod(entry.domain_name, entry.mod_id, entry.version)
                def _done():
                    self._log(f"Endorsed Mods: Abstained from '{entry.name}' ({entry.mod_id}).")
                    # Remove from local list and repaint
                    self._entries = [e for e in self._entries if e.mod_id != entry.mod_id]
                    self._rebuild_buttons()
                    self._repaint()
                    count = len(self._entries)
                    self._status_label.configure(
                        text=f"{count} endorsed mod(s) for {entry.domain_name}"
                    )
                self._parent.after(0, _done)
            except Exception as exc:
                self._parent.after(0,
                    lambda: self._log(f"Endorsed Mods: Abstain failed — {exc}"))

        threading.Thread(target=_worker, daemon=True).start()
