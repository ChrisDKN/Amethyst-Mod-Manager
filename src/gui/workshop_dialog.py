"""
workshop_dialog.py
Converts the current modlist into a Nexus Collections manifest.
Opens as an overlay over the modlist panel (same pattern as CollectionsDialog).

All rows are drawn directly on a canvas (no per-row Tk widgets) so scrolling
is smooth regardless of modlist size.  Clicking a version button opens a
VersionPickerOverlay placed over the workshop itself.
"""

from __future__ import annotations

import json
import zipfile
import tkinter as tk
import tkinter.font as tkfont
import tkinter.messagebox as messagebox
from pathlib import Path
from threading import Thread
from typing import Callable, Optional

import customtkinter as ctk

from Nexus.nexus_meta import read_meta
from Utils.config_paths import get_fomod_selections_path
from Utils.plugins import read_plugins
from Utils.portal_filechooser import pick_save_file
from gui.ctk_components import CTkAlert
from gui.theme import (
    BG_DEEP,
    BG_HEADER,
    BG_PANEL,
    BG_ROW,
    BG_SEP,
    BG_HOVER,
    ACCENT,
    ACCENT_HOV,
    BORDER,
    TEXT_MAIN,
    TEXT_DIM,
    FONT_HEADER,
    FONT_BOLD,
    FONT_SMALL,
    FS11,
    scaled,
    font_sized,
    FONT_FAMILY,
)

_ROW_H   = scaled(26)
_HEADERS = ("Mod Name", "Mod ID", "Preferred Version", "Source", "Fomod", "Optional")

# Fixed widths for the smaller columns.
# Name gets whatever is left over.
_CW_MODID   = scaled(90)
_CW_VER     = scaled(190)
_CW_OPT     = scaled(90)
_CW_FOMOD   = scaled(70)
_CW_SRC     = scaled(90)
_CW_FIXED   = _CW_MODID + _CW_VER + _CW_OPT + _CW_FOMOD + _CW_SRC

def _compute_col_layout(canvas_w: int) -> tuple[tuple[int, ...], tuple[int, ...]]:
    """Return (col_widths, col_x) for the 6 columns given canvas_w."""
    cw_name = max(canvas_w - _CW_FIXED, scaled(150))
    cw = (cw_name, _CW_MODID, _CW_VER, _CW_SRC, _CW_FOMOD, _CW_OPT)
    cx: tuple[int, ...] = (0,)
    for w in cw[:-1]:
        cx = cx + (cx[-1] + w,)
    return cw, cx

# Module-level fallback (used until first canvas configure)
_CW, _CX = _compute_col_layout(scaled(840))

_CB_SIZE = scaled(14)
_CB_PAD  = (_ROW_H - _CB_SIZE) // 2

_tk_font_cache: tkfont.Font | None = None

def _truncate(text: str, max_px: int, font: tkfont.Font) -> str:
    """Return text truncated with '…' so it fits within max_px pixels."""
    if font.measure(text) <= max_px:
        return text
    ellipsis = "…"
    ellipsis_w = font.measure(ellipsis)
    lo, hi = 0, len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if font.measure(text[:mid]) + ellipsis_w <= max_px:
            lo = mid
        else:
            hi = mid - 1
    return text[:lo] + ellipsis


def _norm_ver_name(s: str) -> str:
    """Normalise a mod/file name for match comparison (lower, alnum only)."""
    return "".join(ch for ch in s.lower() if ch.isalnum())


# ---------------------------------------------------------------------------
# VersionPickerOverlay
# ---------------------------------------------------------------------------

class VersionPickerOverlay(tk.Frame):
    """
    Version picker placed over the plugin panel container.
    Two columns: "File ID — Version" and "Name".
    Rows sorted latest-first (caller passes them pre-sorted).
    """

    _POOL  = 40
    _ROW_H = scaled(28)
    _COL0_FRAC = 0.40   # fraction of canvas width for the file-id/version column

    def __init__(
        self,
        parent: tk.Widget,
        mod_name: str,
        entries: list[dict],   # list of {label, name, current}
        on_pick: Callable[[str], None],
        on_close: Callable,
    ):
        super().__init__(parent, bg=BG_DEEP)
        self._ver_entries = entries   # [{label, name}]
        self._on_pick  = on_pick
        self._on_close = on_close
        self._hover_idx = -1
        self._current  = next((e["label"] for e in entries if e.get("current")), "")

        self._pool_bg:    list[int] = []
        self._pool_label: list[int] = []   # file_id — version
        self._pool_name:  list[int] = []   # file display name
        self._pool_slot:  list[int] = []
        self._canvas_w = 400
        self._col0_x = 0
        self._col1_x = int(400 * self._COL0_FRAC)
        self._hdr_labels: list[tk.Label] = []

        self._font = font_sized(FONT_FAMILY, 10)

        self._build_ui(mod_name)
        self._create_pool()
        self.after(10, self._redraw)

    def _build_ui(self, mod_name: str):
        toolbar = tk.Frame(self, bg=BG_HEADER, height=scaled(28))
        toolbar.pack(side="top", fill="x")
        toolbar.pack_propagate(False)

        ctk.CTkButton(
            toolbar, text="✕ Close", width=scaled(72), height=scaled(26),
            fg_color="#b33a3a", hover_color="#c94848", text_color="white",
            font=FONT_HEADER, command=self._on_close,
        ).pack(side="right", padx=(4, 8), pady=2)

        tk.Label(
            toolbar, text=f"Select version — {mod_name}",
            bg=BG_HEADER, fg=TEXT_MAIN, font=FONT_BOLD,
        ).pack(side="left", padx=8)

        self._hdr_frame = tk.Frame(self, bg=BG_HEADER, height=scaled(22))
        self._hdr_frame.pack(side="top", fill="x")
        self._hdr_frame.pack_propagate(False)
        self._hdr_frame.bind("<Configure>", self._on_hdr_configure)
        for text in ("File ID — Version", "Name"):
            lbl = tk.Label(
                self._hdr_frame, text=text, bg=BG_HEADER, fg=TEXT_MAIN,
                font=FONT_BOLD, anchor="w",
            )
            self._hdr_labels.append(lbl)

        body = tk.Frame(self, bg=BG_DEEP)
        body.pack(side="top", fill="both", expand=True)

        self._canvas = tk.Canvas(body, bg=BG_DEEP, highlightthickness=0, bd=0)
        self._canvas.pack(side="left", fill="both", expand=True)

        sb = tk.Scrollbar(
            body, orient="vertical", command=self._canvas.yview,
            bg=BG_SEP, troughcolor=BG_DEEP, activebackground=ACCENT,
            highlightthickness=0, bd=0,
        )
        sb.pack(side="right", fill="y")
        self._canvas.configure(yscrollcommand=self._on_yscroll)
        self._sb = sb

        n = len(self._ver_entries)
        self._canvas.configure(scrollregion=(0, 0, self._canvas_w, n * self._ROW_H))

        self._canvas.bind("<Configure>",     self._on_canvas_configure)
        self._canvas.bind("<MouseWheel>",    lambda e: self._scroll(-(e.delta // 120)))
        self._canvas.bind("<Button-4>",      lambda e: self._scroll(-1))
        self._canvas.bind("<Button-5>",      lambda e: self._scroll(1))
        self._canvas.bind("<ButtonPress-1>", self._on_click)
        self._canvas.bind("<Motion>",        self._on_motion)
        self._canvas.bind("<Leave>",         self._on_leave)

    def _create_pool(self):
        c   = self._canvas
        OFF = -self._ROW_H * 2
        fn  = self._font
        for _ in range(self._POOL):
            bg  = c.create_rectangle(0, OFF, 0, OFF, fill=BG_DEEP, outline="", state="hidden")
            lbl = c.create_text(self._col0_x + scaled(4), OFF, text="", anchor="w",
                                fill=TEXT_MAIN, font=fn, state="hidden")
            nm  = c.create_text(self._col1_x + scaled(4), OFF, text="", anchor="w",
                                fill=TEXT_DIM,  font=fn, state="hidden")
            self._pool_bg.append(bg)
            self._pool_label.append(lbl)
            self._pool_name.append(nm)
            self._pool_slot.append(-1)

    def _redraw(self):
        n = len(self._ver_entries)
        if not n:
            return
        c          = self._canvas
        canvas_top = int(c.canvasy(0))
        canvas_h   = max(c.winfo_height(), 1)
        first_row  = max(0, canvas_top // self._ROW_H)
        last_row   = min(n, (canvas_top + canvas_h) // self._ROW_H + 2)
        wanted     = set(range(first_row, last_row))

        showing: dict[int, int] = {}
        free:    list[int]      = []
        for s in range(self._POOL):
            di = self._pool_slot[s]
            if di != -1 and di in wanted:
                showing[di] = s
            else:
                if di != -1:
                    for lst in (self._pool_bg, self._pool_label, self._pool_name):
                        c.itemconfigure(lst[s], state="hidden")
                    self._pool_slot[s] = -1
                free.append(s)

        fi = 0
        for di in range(first_row, last_row):
            if di in showing:
                s  = showing[di]
                yc = di * self._ROW_H + self._ROW_H // 2
                c.coords(self._pool_bg[s], 0, di * self._ROW_H, self._canvas_w, di * self._ROW_H + self._ROW_H)
                c.coords(self._pool_label[s], self._col0_x + scaled(4), yc)
                c.coords(self._pool_name[s],  self._col1_x + scaled(4), yc)
                self._apply_row_bg(s, di)
                continue
            if fi >= len(free):
                break
            s = free[fi]; fi += 1
            entry = self._ver_entries[di]
            y0    = di * self._ROW_H
            y1    = y0 + self._ROW_H
            yc    = y0 + self._ROW_H // 2

            self._pool_slot[s] = di
            c.coords(self._pool_bg[s], 0, y0, self._canvas_w, y1)
            self._apply_row_bg(s, di)
            c.itemconfigure(self._pool_bg[s], state="normal")

            is_cur = entry["label"] == self._current
            is_match = bool(entry.get("matches"))
            if is_cur:
                fg, fg2 = "white", "white"
            elif is_match:
                fg, fg2 = "#5cd65c", "#5cd65c"
            else:
                fg, fg2 = TEXT_MAIN, TEXT_DIM

            c.coords(self._pool_label[s], self._col0_x + scaled(4), yc)
            c.itemconfigure(self._pool_label[s], text=entry["label"], fill=fg, state="normal")

            c.coords(self._pool_name[s], self._col1_x + scaled(4), yc)
            c.itemconfigure(self._pool_name[s], text=entry["name"], fill=fg2, state="normal")

    def _apply_row_bg(self, s: int, di: int):
        entry  = self._ver_entries[di]
        is_cur = entry["label"] == self._current
        is_match = bool(entry.get("matches"))
        if is_cur:
            fill = ACCENT
        elif di == self._hover_idx:
            fill = BG_HOVER
        else:
            fill = BG_ROW if di % 2 == 0 else BG_DEEP
        self._canvas.itemconfigure(self._pool_bg[s], fill=fill)
        if is_cur:
            fg, fg2 = "white", "white"
        elif is_match:
            fg, fg2 = "#5cd65c", "#5cd65c"
        else:
            fg, fg2 = TEXT_MAIN, TEXT_DIM
        self._canvas.itemconfigure(self._pool_label[s], fill=fg)
        self._canvas.itemconfigure(self._pool_name[s],  fill=fg2)

    def _on_click(self, event):
        cy = int(self._canvas.canvasy(event.y))
        di = cy // self._ROW_H
        if 0 <= di < len(self._ver_entries):
            self._on_pick(self._ver_entries[di]["label"])
            self._on_close()

    def _on_motion(self, event):
        cy = int(self._canvas.canvasy(event.y))
        di = cy // self._ROW_H
        if di != self._hover_idx:
            old = self._hover_idx
            self._hover_idx = di
            for s in range(self._POOL):
                if self._pool_slot[s] in (old, di):
                    self._apply_row_bg(s, self._pool_slot[s])

    def _on_leave(self, _event):
        old = self._hover_idx
        self._hover_idx = -1
        for s in range(self._POOL):
            if self._pool_slot[s] == old:
                self._apply_row_bg(s, old)

    def _scroll(self, units: int):
        self._canvas.yview_scroll(units * self._ROW_H, "units")
        self.after_idle(self._redraw)

    def _on_yscroll(self, *args):
        self._sb.set(*args)
        self.after_idle(self._redraw)

    def _update_columns(self, width: int):
        self._canvas_w = width
        self._col0_x = 0
        self._col1_x = int(width * self._COL0_FRAC)
        # Reposition header labels
        if self._hdr_labels:
            col0_w = self._col1_x - scaled(4)
            col1_w = width - self._col1_x - scaled(4)
            self._hdr_labels[0].place(x=scaled(4), y=scaled(2), width=max(col0_w, 1), height=scaled(18))
            self._hdr_labels[1].place(x=self._col1_x + scaled(4), y=scaled(2), width=max(col1_w, 1), height=scaled(18))

    def _on_hdr_configure(self, event):
        self._update_columns(event.width)

    def _on_canvas_configure(self, event):
        self._update_columns(event.width)
        n = len(self._ver_entries)
        if n:
            self._canvas.configure(scrollregion=(0, 0, event.width, n * self._ROW_H))
        self.after_idle(self._redraw)


# ---------------------------------------------------------------------------
# SourcePickerOverlay
# ---------------------------------------------------------------------------

def _source_btn_style(source: str) -> tuple[str, str]:
    """Return (bg_colour, label_text) for the source column button."""
    if source == "direct":
        return "#5a7a5a", "Direct"
    if source == "bundle":
        return "#7a5a7a", "Bundle"
    return "#c77a3a", "Nexus"


class SourcePickerOverlay(tk.Frame):
    """
    Small overlay for selecting the download source of a mod.
    Options: Nexus (default), Direct URL, or Bundle (included in archive).
    """

    def __init__(
        self,
        parent: tk.Widget,
        mod_name: str,
        current_source: str,
        current_url: str,
        on_pick: Callable[[str, str], None],   # (source, url)
        on_close: Callable,
    ):
        super().__init__(parent, bg=BG_DEEP)
        self._on_pick  = on_pick
        self._on_close = on_close
        self._source_var = tk.StringVar(value=current_source)
        self._url_var    = tk.StringVar(value=current_url)
        self._build_ui(mod_name)

    def _build_ui(self, mod_name: str):
        toolbar = tk.Frame(self, bg=BG_HEADER, height=scaled(28))
        toolbar.pack(side="top", fill="x")
        toolbar.pack_propagate(False)

        ctk.CTkButton(
            toolbar, text="✕ Close", width=scaled(72), height=scaled(26),
            fg_color="#b33a3a", hover_color="#c94848", text_color="white",
            font=FONT_HEADER, command=self._on_close,
        ).pack(side="right", padx=(4, 8), pady=2)

        ctk.CTkButton(
            toolbar, text="Apply", width=scaled(72), height=scaled(26),
            fg_color=ACCENT, hover_color=ACCENT_HOV, text_color="white",
            font=FONT_HEADER, command=self._apply,
        ).pack(side="right", padx=(0, 4), pady=2)

        tk.Label(
            toolbar, text=f"Source — {mod_name}",
            bg=BG_HEADER, fg=TEXT_MAIN, font=FONT_BOLD,
        ).pack(side="left", padx=8)

        body = tk.Frame(self, bg=BG_DEEP)
        body.pack(side="top", fill="both", expand=True, padx=scaled(16), pady=scaled(16))

        for value, label, desc in (
            ("nexus",  "Nexus Mods",  "Download mod from Nexus"),
            ("direct", "Direct URL",  "For off-site mods"),
            ("bundle", "Bundle",      "Include mod in the output (e.g. DynDOLOD output)"),
        ):
            row = tk.Frame(body, bg=BG_DEEP)
            row.pack(fill="x", pady=(0, scaled(6)))
            ctk.CTkRadioButton(
                row, text=label, variable=self._source_var, value=value,
                font=FONT_BOLD, fg_color=ACCENT, hover_color=ACCENT_HOV,
                command=self._on_radio_change,
            ).pack(side="left")
            tk.Label(
                row, text=f"— {desc}", bg=BG_DEEP, fg=TEXT_DIM, font=FONT_SMALL,
            ).pack(side="left", padx=(scaled(6), 0))

        # URL entry (shown only when Direct is selected)
        self._url_frame = tk.Frame(body, bg=BG_DEEP)

        tk.Label(
            self._url_frame, text="Download URL:", bg=BG_DEEP, fg=TEXT_DIM, font=FONT_SMALL,
        ).pack(side="left", padx=(scaled(24), scaled(6)))

        self._url_entry = ctk.CTkEntry(
            self._url_frame, textvariable=self._url_var,
            width=scaled(340), height=scaled(26),
            font=FONT_SMALL, placeholder_text="https://…",
        )
        self._url_entry.pack(side="left")

        self._on_source_change()

    def _on_source_change(self):
        if self._source_var.get() == "direct":
            self._url_frame.pack(fill="x", pady=(0, scaled(4)))
        else:
            self._url_frame.pack_forget()

    def _on_radio_change(self):
        self._on_source_change()
        if self._source_var.get() != "direct":
            self._apply()

    def _apply(self):
        source = self._source_var.get()
        url    = self._url_var.get().strip() if source == "direct" else ""
        self._on_pick(source, url)
        self._on_close()


# ---------------------------------------------------------------------------
# WorkshopDialog
# ---------------------------------------------------------------------------

class WorkshopDialog(tk.Frame):
    """
    Workshop overlay.  Pure-canvas rendering — no per-row widgets.
    """

    _POOL = 64

    def __init__(
        self,
        parent: tk.Widget,
        entries: list,
        game,
        api,
        game_domain: str,
        on_close: Optional[Callable] = None,
        overlay_parent: Optional[tk.Widget] = None,
    ):
        super().__init__(parent, bg=BG_DEEP)
        self._entries        = entries
        self._game           = game
        self._api            = api
        self._game_domain    = game_domain
        self._on_close       = on_close
        self._overlay_parent = overlay_parent  # plugin panel container

        self._rows: list[dict] = []
        self._all_rows: list[dict] = []
        self._hide_no_fileid_var = tk.BooleanVar(value=False)

        self._pool_bg:      list[int] = []
        self._pool_name:    list[int] = []
        self._pool_modid:   list[int] = []
        self._pool_ver:     list[int] = []
        self._pool_ver_btn: list[int] = []
        self._pool_cb_rect:    list[int] = []
        self._pool_cb_mark:    list[int] = []
        self._pool_fomod_rect: list[int] = []
        self._pool_fomod_mark: list[int] = []
        self._pool_src_btn:    list[int] = []
        self._pool_src_lbl:    list[int] = []
        self._pool_slot:       list[int] = []

        self._source_overlay: Optional[SourcePickerOverlay] = None

        self._canvas_w  = scaled(840)
        self._col_cw, self._col_cx = _compute_col_layout(self._canvas_w)
        self._font_main = font_sized(FONT_FAMILY, 10, "bold")
        self._font_small = font_sized(FONT_FAMILY, 9, "bold")

        global _tk_font_cache
        if _tk_font_cache is None:
            _tk_font_cache = tkfont.Font(family=FONT_FAMILY, size=10)

        self._version_overlay: Optional[VersionPickerOverlay] = None

        self._build_ui()
        self._create_pool()
        self.after(50, self._load_mods)

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------

    def _build_ui(self):
        self.grid_rowconfigure(0, weight=0)
        self.grid_rowconfigure(1, weight=0)
        self.grid_rowconfigure(2, weight=1)
        self.grid_rowconfigure(3, weight=0)
        self.grid_columnconfigure(0, weight=1)

        toolbar = tk.Frame(self, bg=BG_HEADER, height=scaled(28))
        toolbar.grid(row=0, column=0, sticky="ew")
        toolbar.grid_propagate(False)

        ctk.CTkButton(
            toolbar, text="✕ Close", width=scaled(72), height=scaled(26),
            fg_color="#b33a3a", hover_color="#c94848", text_color="white",
            font=FONT_HEADER, command=self._do_close,
        ).pack(side="right", padx=(4, 8), pady=2)

        ctk.CTkButton(
            toolbar, text="Export", width=scaled(72), height=scaled(26),
            fg_color=ACCENT, hover_color=ACCENT_HOV, text_color="white",
            font=FONT_HEADER, command=self._do_export,
        ).pack(side="right", padx=(0, 4), pady=2)

        ctk.CTkButton(
            toolbar, text="Load", width=scaled(60), height=scaled(26),
            fg_color=ACCENT, hover_color=ACCENT_HOV, text_color="white",
            font=FONT_HEADER, command=self._do_load,
        ).pack(side="right", padx=(0, 4), pady=2)

        ctk.CTkButton(
            toolbar, text="Save", width=scaled(60), height=scaled(26),
            fg_color=ACCENT, hover_color=ACCENT_HOV, text_color="white",
            font=FONT_HEADER, command=self._do_save,
        ).pack(side="right", padx=(0, 4), pady=2)

        tk.Label(
            toolbar, text="Workshop", bg=BG_HEADER, fg=TEXT_MAIN,
            font=FONT_BOLD,
        ).pack(side="left", padx=8)

        tk.Checkbutton(
            toolbar, text="Only show mods without file ID",
            variable=self._hide_no_fileid_var,
            bg=BG_HEADER, fg=TEXT_MAIN, selectcolor=BG_DEEP,
            activebackground=BG_HEADER, activeforeground=TEXT_MAIN,
            font=FONT_SMALL, bd=0, highlightthickness=0,
            command=self._apply_filter,
        ).pack(side="left", padx=(12, 0))

        search_bar = tk.Frame(self, bg=BG_DEEP, bd=0, highlightthickness=0, height=scaled(32))
        search_bar.grid(row=3, column=0, sticky="ew")
        search_bar.grid_propagate(False)

        tk.Label(search_bar, text="🔍", bg=BG_DEEP, fg=TEXT_DIM,
                 font=(FONT_FAMILY, FS11)).pack(side="left", padx=(8, 2), pady=4)

        self._search_entry = tk.Entry(
            search_bar,
            bg=BG_PANEL, fg=TEXT_MAIN, insertbackground=TEXT_MAIN,
            relief="flat", font=(FONT_FAMILY, FS11),
            bd=0, highlightthickness=1,
            highlightbackground=BORDER, highlightcolor=ACCENT,
        )
        self._search_entry.pack(side="left", fill="x", expand=True, padx=(2, 2), pady=4)

        self._search_clear_btn = ctk.CTkButton(
            search_bar, text="✕", width=scaled(32), height=scaled(24),
            fg_color="#b33a3a", hover_color="#c94848", text_color="white",
            font=FONT_HEADER, cursor="hand2",
            command=self._on_search_clear,
        )
        self._search_clear_btn.pack(side="left", padx=(0, 8), pady=4)
        self._search_clear_btn.pack_forget()

        self._search_entry.bind("<KeyRelease>", self._on_search_change)
        self._search_entry.bind("<Escape>", self._on_search_clear)
        self._search_entry.bind("<Control-a>", lambda e: (
            self._search_entry.select_range(0, "end"),
            self._search_entry.icursor("end"),
            "break"
        )[-1])

        self._search_text = ""

        self._hdr_frame = tk.Frame(self, bg=BG_HEADER, height=scaled(22))
        self._hdr_frame.grid(row=1, column=0, sticky="ew")
        self._hdr_frame.grid_propagate(False)
        self._hdr_labels: list[tk.Label] = []
        for text in _HEADERS:
            lbl = tk.Label(
                self._hdr_frame, text=text, bg=BG_HEADER, fg=TEXT_MAIN,
                font=FONT_BOLD, anchor="w",
            )
            self._hdr_labels.append(lbl)
        self._update_header()

        body = tk.Frame(self, bg=BG_DEEP)
        body.grid(row=2, column=0, sticky="nsew")
        body.grid_rowconfigure(0, weight=1)
        body.grid_columnconfigure(0, weight=1)

        self._canvas = tk.Canvas(body, bg=BG_DEEP, highlightthickness=0, bd=0)
        self._canvas.grid(row=0, column=0, sticky="nsew")

        sb = tk.Scrollbar(
            body, orient="vertical", command=self._canvas.yview,
            bg=BG_SEP, troughcolor=BG_DEEP, activebackground=ACCENT,
            highlightthickness=0, bd=0,
        )
        sb.grid(row=0, column=1, sticky="ns")
        self._canvas.configure(yscrollcommand=self._on_yscroll)
        self._sb = sb

        self._canvas.bind("<Configure>",     self._on_canvas_configure)
        self._canvas.bind("<MouseWheel>",    lambda e: self._scroll(-(e.delta // 120)))
        self._canvas.bind("<Button-4>",      lambda e: self._scroll(-1))
        self._canvas.bind("<Button-5>",      lambda e: self._scroll(1))
        self._canvas.bind("<ButtonPress-1>", self._on_canvas_click)

    def _do_close(self):
        self._close_version_overlay()
        self._close_source_overlay()
        if self._on_close:
            self._on_close()
        else:
            self.destroy()

    def _workshop_dir(self) -> "Path | None":
        profile_dir = getattr(self._game, "_active_profile_dir", None)
        if not profile_dir:
            return None
        d = Path(profile_dir) / "workshop"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _do_save(self):
        if not self._rows:
            CTkAlert(state="warning", title="Workshop",
                     body_text="Nothing to save.", btn1="OK", btn2="",
                     parent=self.winfo_toplevel()).get()
            return
        ws_dir = self._workshop_dir()
        if not ws_dir:
            CTkAlert(state="error", title="Workshop",
                     body_text="No active profile.", btn1="OK", btn2="",
                     parent=self.winfo_toplevel()).get()
            return

        # Save directly to the profile's workshop folder with a timestamped name.
        from datetime import datetime
        fname = f"workshop_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        self._write_settings(ws_dir / fname)

    def _write_settings(self, out_path):
        if not out_path:
            return
        try:
            out_path = Path(out_path)
            if out_path.suffix.lower() != ".json":
                out_path = out_path.with_suffix(".json")
            def _row_file_id(r):
                fid = r.get("file_id") or 0
                if not fid:
                    lbl = r.get("ver_label", "")
                    if lbl and " — " in lbl:
                        try:
                            fid = int(lbl.split(" — ")[0])
                        except ValueError:
                            fid = 0
                return fid

            data = {
                "version": 1,
                "mods": [
                    {
                        "name":       r["name"],
                        "optional":   r["optional"],
                        "source":     r.get("source", "nexus"),
                        "direct_url": r.get("direct_url", ""),
                        "file_id":    _row_file_id(r),
                        "ver_label":  r.get("ver_label", "—"),
                    }
                    for r in self._all_rows
                ],
            }
            with open(out_path, "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=2)
            CTkAlert(state="info", title="Workshop",
                     body_text=f"Settings saved to:\n{out_path}",
                     btn1="OK", btn2="", parent=self.winfo_toplevel()).get()
        except Exception as exc:
            CTkAlert(state="error", title="Workshop",
                     body_text=f"Save failed:\n{exc}",
                     btn1="OK", btn2="", parent=self.winfo_toplevel()).get()

    def _do_load(self):
        ws_dir = self._workshop_dir()
        if not ws_dir:
            CTkAlert(state="error", title="Workshop",
                     body_text="No active profile.", btn1="OK", btn2="",
                     parent=self.winfo_toplevel()).get()
            return

        files = sorted(
            ws_dir.glob("*.json"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        if not files:
            CTkAlert(state="info", title="Workshop",
                     body_text="No saved settings found.",
                     btn1="OK", btn2="", parent=self.winfo_toplevel()).get()
            return

        top = tk.Toplevel(self)
        top.title("Load Workshop Settings")
        top.configure(bg=BG_DEEP)
        top.transient(self)
        top.after(50, lambda: (top.grab_set() if top.winfo_viewable() else None))

        tk.Label(
            top, text="Select a saved workshop file:",
            bg=BG_DEEP, fg=TEXT_MAIN, font=FONT_BOLD,
        ).pack(padx=12, pady=(12, 6), anchor="w")

        lb = tk.Listbox(
            top, width=50, height=min(15, max(4, len(files))),
            bg=BG_ROW, fg=TEXT_MAIN, selectbackground=ACCENT,
            highlightthickness=0, bd=0, font=FONT_SMALL,
        )
        for f in files:
            lb.insert("end", f.name)
        lb.selection_set(0)
        lb.pack(padx=12, pady=4, fill="both", expand=True)

        def _load_selected():
            sel = lb.curselection()
            if not sel:
                return
            path = files[sel[0]]
            top.destroy()
            self._read_settings(path)

        btns = tk.Frame(top, bg=BG_DEEP)
        btns.pack(fill="x", padx=12, pady=(6, 12))
        ctk.CTkButton(
            btns, text="Load", width=scaled(80), height=scaled(26),
            fg_color=ACCENT, hover_color=ACCENT_HOV, text_color="white",
            font=FONT_HEADER, command=_load_selected,
        ).pack(side="right", padx=(4, 0))
        ctk.CTkButton(
            btns, text="Cancel", width=scaled(80), height=scaled(26),
            fg_color="#b33a3a", hover_color="#c94848", text_color="white",
            font=FONT_HEADER, command=top.destroy,
        ).pack(side="right")

        lb.bind("<Double-Button-1>", lambda _e: _load_selected())

    def _read_settings(self, in_path, silent: bool = False):
        if not in_path:
            return
        try:
            with open(in_path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            by_name = {m["name"]: m for m in data.get("mods", [])}
            for row in self._all_rows:
                m = by_name.get(row["name"])
                if not m:
                    continue
                row["optional"]   = bool(m.get("optional", False))
                row["source"]     = m.get("source", "nexus")
                row["direct_url"] = m.get("direct_url", "")
                # Only apply file_id / ver_label from the JSON when the mod has no
                # file_id already set from meta.ini — the installed file takes precedence.
                if not row.get("file_id"):
                    if m.get("file_id"):
                        row["file_id"] = m["file_id"]
                    if m.get("ver_label"):
                        row["ver_label"] = m["ver_label"]
                        # Back-fill file_id from ver_label ("fileid — version") if missing.
                        if not row.get("file_id") and " — " in row["ver_label"]:
                            try:
                                row["file_id"] = int(row["ver_label"].split(" — ")[0])
                            except ValueError:
                                pass
            self._apply_filter()
            if not silent:
                CTkAlert(state="info", title="Workshop",
                         body_text="Settings loaded.",
                         btn1="OK", btn2="", parent=self.winfo_toplevel()).get()
        except Exception as exc:
            if not silent:
                CTkAlert(state="error", title="Workshop",
                         body_text=f"Load failed:\n{exc}",
                         btn1="OK", btn2="", parent=self.winfo_toplevel()).get()

    def _do_export(self):
        if not self._all_rows:
            CTkAlert(state="warning", title="Workshop",
                     body_text="No mods to export.", btn1="OK", btn2="",
                     parent=self.winfo_toplevel()).get()
            return

        missing = [
            row["name"] for row in self._all_rows
            if row.get("source", "nexus") == "nexus" and not row.get("file_id")
        ]
        if missing:
            count = len(missing)
            noun = "mod" if count == 1 else "mods"
            verb = "is" if count == 1 else "are"
            CTkAlert(
                state="warning", title="Workshop",
                body_text=(
                    f"{count} Nexus {noun} {verb} missing a File ID and must be set before exporting."
                ),
                btn1="OK", btn2="",
                parent=self.winfo_toplevel(),
            ).get()
            return

        profile_dir = getattr(self._game, "_active_profile_dir", None)
        profile_name = Path(profile_dir).name if profile_dir else "manifest"
        default_name = f"{profile_name}_export.amethyst"

        pick_save_file(
            "Export Amethyst Manifest",
            lambda p: self.after(0, lambda: self._prefetch_sizes_then_export(p)),
            current_name=default_name,
            filters=[("Amethyst Manifest (*.amethyst)", ["*.amethyst"]), ("All files", ["*"])],
        )

    def _prefetch_sizes_then_export(self, out_path):
        """
        Fetch file sizes for rows that don't have one yet using a single batched
        GraphQL request, then write the manifest.
        """
        if not out_path:
            return

        # Collect rows that have mod_id + file_id but no size yet
        needs_size = [
            row for row in self._all_rows
            if row["mod_id"] and row["file_id"] and not row["size_bytes"]
        ]

        if not needs_size:
            self._write_manifest(out_path)
            return

        # Show status while the single batch request is in-flight
        self._export_status_var = tk.StringVar(value="Fetching file sizes…")
        status_lbl = tk.Label(
            self, textvariable=self._export_status_var,
            bg=BG_DEEP, fg=TEXT_DIM, font=FONT_SMALL,
        )
        status_lbl.place(relx=0.0, rely=1.0, anchor="sw", x=8, y=-4)

        pairs = [(row["mod_id"], row["file_id"]) for row in needs_size]

        def run():
            try:
                size_map = self._api.graphql_file_sizes_batch(self._game_domain, pairs)
            except Exception:
                size_map = {}
            self.after(0, lambda: _apply(size_map))

        def _apply(size_map):
            for row in needs_size:
                sz = size_map.get((row["mod_id"], row["file_id"]), 0)
                if sz:
                    row["size_bytes"] = sz
            try:
                status_lbl.destroy()
            except Exception:
                pass
            self._write_manifest(out_path)

        Thread(target=run, daemon=True).start()

    def _write_manifest(self, out_path: "Path | None"):
        if not out_path:
            return

        game_name   = self._game.name if self._game else None
        profile_dir = getattr(self._game, "_active_profile_dir", None)

        mods = []
        for row in self._all_rows:
            # Parse fileid from ver_label (format "fileid — version" or just "—")
            ver_label = row["ver_label"]
            file_id   = row["file_id"]
            if ver_label and " — " in ver_label:
                try:
                    file_id = int(ver_label.split(" — ")[0])
                except ValueError:
                    pass

            row_source = row.get("source", "nexus")
            if row_source == "direct":
                source: dict = {
                    "type": "direct",
                    "url":  row.get("direct_url", ""),
                }
            elif row_source == "bundle":
                source = {"bundle": True}
            else:
                source = {
                    "modId":  row["mod_id"],
                    "fileId": file_id,
                    "logicalFilename": row["name"],
                }
                if row.get("size_bytes"):
                    source["fileSize"] = row["size_bytes"]

            mod_entry: dict = {
                "name":     row["name"],
                "source":   source,
                "optional": row["optional"],
            }

            # Include version and category from meta.ini if available.
            row_version = row.get("version") or ""
            if not row_version and ver_label and " — " in ver_label:
                row_version = ver_label.split(" — ", 1)[1]
            if row_version:
                mod_entry["version"] = row_version
            cat_id   = row.get("category_id") or 0
            cat_name = row.get("category_name") or ""
            if cat_id or cat_name:
                mod_entry["category"] = {}
                if cat_id:
                    mod_entry["category"]["id"] = cat_id
                if cat_name:
                    mod_entry["category"]["name"] = cat_name

            if row["has_fomod"] and row.get("fomod_export", True) and game_name:
                # Prefer the profile-local copy so manifest exports stay
                # profile-specific even if the global fomod settings differ.
                fomod_path = None
                if profile_dir:
                    candidate = Path(profile_dir) / "fomod" / f"{row['name']}.json"
                    if candidate.is_file():
                        fomod_path = candidate
                if fomod_path is None:
                    fomod_path = get_fomod_selections_path(game_name, row["name"])
                if fomod_path.is_file():
                    try:
                        with fomod_path.open("r", encoding="utf-8") as fh:
                            fomod_data = json.load(fh)
                        mod_entry["choices"] = {
                            "type":        "fomod_selections",
                            "selections":  fomod_data,
                        }
                    except Exception:
                        pass

            mods.append(mod_entry)

        try:
            from version import __version__ as _app_version
        except Exception:
            _app_version = ""

        manifest = {
            "AmethystManifest": True,
            "info": {
                "domainName": self._game_domain,
                "appVersion": _app_version,
            },
            "mods":             mods,
        }

        try:
            out_path = Path(out_path)
            if out_path.suffix.lower() not in (".zip", ".amethyst"):
                out_path = out_path.with_suffix(".amethyst")

            staging_root = (
                self._game.get_effective_mod_staging_path() if self._game else None
            )
            overwrite_root = (
                self._game.get_effective_overwrite_path() if self._game else None
            )

            bundle_names = [
                r["name"] for r in self._rows if r.get("source") == "bundle"
            ]

            with zipfile.ZipFile(
                out_path, "w", compression=zipfile.ZIP_DEFLATED
            ) as zf:
                zf.writestr(
                    "manifest.json",
                    json.dumps(manifest, indent=2),
                )

                if staging_root:
                    for name in bundle_names:
                        mod_dir = staging_root / name
                        if not mod_dir.is_dir():
                            continue
                        for fp in mod_dir.rglob("*"):
                            if fp.is_file():
                                arcname = Path("mods") / name / fp.relative_to(mod_dir)
                                zf.write(fp, arcname.as_posix())

                if overwrite_root and overwrite_root.is_dir():
                    for fp in overwrite_root.rglob("*"):
                        if fp.is_file():
                            arcname = Path("overwrite") / fp.relative_to(overwrite_root)
                            zf.write(fp, arcname.as_posix())

                # Bundle profile state files: fixed names + any *.ini files.
                if profile_dir:
                    pdir = Path(profile_dir)
                    fixed = [
                        "modlist.txt",
                        "plugins.txt",
                        "loadorder.txt",
                        "profile_state.json",
                        "userlist.yaml",
                    ]
                    for fname in fixed:
                        fp = pdir / fname
                        if not fp.is_file():
                            continue
                        if fname == "profile_state.json":
                            # Inject profile_specific_mods=true if missing.
                            try:
                                ps = json.loads(fp.read_text(encoding="utf-8"))
                            except Exception:
                                ps = {}
                            if not isinstance(ps, dict):
                                ps = {}
                            settings = ps.get("profile_settings")
                            if not isinstance(settings, dict):
                                settings = {}
                                ps["profile_settings"] = settings
                            if not settings.get("profile_specific_mods"):
                                settings["profile_specific_mods"] = True
                            zf.writestr(
                                (Path("profile") / fname).as_posix(),
                                json.dumps(ps, indent=2),
                            )
                        else:
                            zf.write(fp, (Path("profile") / fname).as_posix())
                    for fp in pdir.glob("*.ini"):
                        if fp.is_file():
                            zf.write(
                                fp,
                                (Path("profile") / fp.name).as_posix(),
                            )

            CTkAlert(state="info", title="Workshop",
                     body_text=f"Manifest exported to:\n{out_path}",
                     btn1="OK", btn2="", parent=self.winfo_toplevel()).get()
        except Exception as exc:
            CTkAlert(state="error", title="Workshop",
                     body_text=f"Export failed:\n{exc}",
                     btn1="OK", btn2="", parent=self.winfo_toplevel()).get()

    # ------------------------------------------------------------------
    # Pool
    # ------------------------------------------------------------------

    def _create_pool(self):
        c   = self._canvas
        FN  = self._font_main
        FS  = self._font_small
        OFF = -_ROW_H * 2

        for _ in range(self._POOL):
            bg   = c.create_rectangle(0, OFF, 0, OFF, fill=BG_DEEP, outline="", state="hidden")
            name = c.create_text(0, OFF, text="", anchor="w", fill=TEXT_MAIN, font=FN, state="hidden")
            mid  = c.create_text(0, OFF, text="", anchor="w", fill=TEXT_DIM,  font=FN, state="hidden")
            vbtn = c.create_rectangle(0, OFF, 0, OFF, fill=ACCENT, outline="", state="hidden")
            ver  = c.create_text(0, OFF, text="", anchor="w", fill="white",   font=FS, state="hidden")
            cbr  = c.create_rectangle(0, OFF, 0, OFF, outline=BORDER, fill="", state="hidden")
            cbm  = c.create_text(0, OFF, text="✓", anchor="center", fill=ACCENT, font=FN, state="hidden")
            fmr  = c.create_rectangle(0, OFF, 0, OFF, outline=BORDER, fill="", state="hidden")
            fmm  = c.create_text(0, OFF, text="✓", anchor="center", fill=ACCENT, font=FN, state="hidden")
            sbtn = c.create_rectangle(0, OFF, 0, OFF, fill=BG_SEP, outline="", state="hidden")
            slbl = c.create_text(0, OFF, text="", anchor="center", fill=TEXT_MAIN, font=FS, state="hidden")

            self._pool_bg.append(bg)
            self._pool_name.append(name)
            self._pool_modid.append(mid)
            self._pool_ver_btn.append(vbtn)
            self._pool_ver.append(ver)
            self._pool_cb_rect.append(cbr)
            self._pool_cb_mark.append(cbm)
            self._pool_fomod_rect.append(fmr)
            self._pool_fomod_mark.append(fmm)
            self._pool_src_btn.append(sbtn)
            self._pool_src_lbl.append(slbl)
            self._pool_slot.append(-1)

    # ------------------------------------------------------------------
    # Data loading
    # ------------------------------------------------------------------

    def _load_mods(self):
        staging_root = self._game.get_effective_mod_staging_path() if self._game else None
        game_name    = self._game.name if self._game else None

        for entry in self._entries:
            mod_id = file_id = 0
            version = ""
            category_id = 0
            category_name = ""
            if staging_root:
                meta_path = staging_root / entry.name / "meta.ini"
                if meta_path.is_file():
                    try:
                        meta          = read_meta(meta_path)
                        mod_id        = meta.mod_id       or 0
                        file_id       = meta.file_id      or 0
                        version       = meta.version      or ""
                        category_id   = meta.category_id  or 0
                        category_name = meta.category_name or ""
                    except Exception:
                        pass

            if file_id and version:
                ver_label = f"{file_id} — {version}"
            elif file_id:
                ver_label = str(file_id)
            else:
                ver_label = "—"

            profile_dir = getattr(self._game, "_active_profile_dir", None)
            has_fomod = bool(
                profile_dir
                and (Path(profile_dir) / "fomod" / f"{entry.name}.json").is_file()
            )

            self._all_rows.append({
                "name":             entry.name,
                "mod_id":           mod_id,
                "file_id":          file_id,
                "version":          version,
                "category_id":      category_id,
                "category_name":    category_name,
                "ver_label":        ver_label,
                "ver_options":      [{"label": ver_label, "name": "", "size_bytes": 0}],
                "optional":         False,
                "has_fomod":        has_fomod,
                "fomod_export":     has_fomod,
                "versions_fetched": False,
                "size_bytes":       0,
                "source":           "nexus",
                "direct_url":       "",
            })

        self._apply_filter()
        self._auto_load_latest()

    def _auto_load_latest(self):
        ws_dir = self._workshop_dir()
        if not ws_dir:
            return
        files = sorted(ws_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        if files:
            try:
                print(f"[workshop] auto-loaded state file: {files[0].name}")
            except Exception:
                pass
            self._read_settings(files[0], silent=True)

    def _on_search_change(self, event=None):
        self._search_text = self._search_entry.get().lower()
        if self._search_text:
            self._search_clear_btn.pack(side="left", padx=(0, 8), pady=4)
        else:
            self._search_clear_btn.pack_forget()
        self._apply_filter()

    def _on_search_clear(self, event=None):
        self._search_entry.delete(0, "end")
        self._search_text = ""
        self._search_clear_btn.pack_forget()
        self._apply_filter()

    def _apply_filter(self):
        if self._hide_no_fileid_var.get():
            rows = [r for r in self._all_rows if not r.get("file_id")]
        else:
            rows = list(self._all_rows)
        if self._search_text:
            q = self._search_text
            rows = [r for r in rows if q in r["name"].lower()]
        rows.sort(key=lambda r: r["name"].lower())
        self._rows = rows

        # Reset pool slots since data_idx mapping changed.
        for s in range(self._POOL):
            if self._pool_slot[s] != -1:
                self._hide_slot(s)

        n = len(self._rows)
        self._canvas.configure(scrollregion=(0, 0, self._canvas_w, n * _ROW_H))
        self._canvas.yview_moveto(0)
        self._redraw()

    # ------------------------------------------------------------------
    # Rendering
    # ------------------------------------------------------------------

    def _redraw(self):
        n = len(self._rows)
        if not n:
            return

        c          = self._canvas
        canvas_top = int(c.canvasy(0))
        canvas_h   = max(c.winfo_height(), 1)
        first_row  = max(0, canvas_top // _ROW_H)
        last_row   = min(n, (canvas_top + canvas_h) // _ROW_H + 2)
        wanted     = set(range(first_row, last_row))

        showing: dict[int, int] = {}
        free:    list[int]      = []

        for s in range(self._POOL):
            di = self._pool_slot[s]
            if di != -1 and di in wanted:
                showing[di] = s
            else:
                if di != -1:
                    self._hide_slot(s)
                free.append(s)

        fi = 0
        for data_idx in range(first_row, last_row):
            if data_idx in showing:
                continue
            if fi >= len(free):
                break
            s = free[fi]; fi += 1
            self._fill_slot(s, data_idx)

    def _hide_slot(self, s: int):
        c = self._canvas
        for lst in (self._pool_bg, self._pool_name, self._pool_modid,
                    self._pool_ver_btn, self._pool_ver,
                    self._pool_cb_rect, self._pool_cb_mark,
                    self._pool_fomod_rect, self._pool_fomod_mark,
                    self._pool_src_btn, self._pool_src_lbl):
            c.itemconfigure(lst[s], state="hidden")
        self._pool_slot[s] = -1

    def _fill_slot(self, s: int, data_idx: int):
        row = self._rows[data_idx]
        c   = self._canvas
        CW  = self._col_cw
        CX  = self._col_cx
        y0  = data_idx * _ROW_H
        y1  = y0 + _ROW_H
        yc  = y0 + _ROW_H // 2
        W   = self._canvas_w
        bg  = BG_ROW if data_idx % 2 == 0 else BG_DEEP

        c.coords(self._pool_bg[s], 0, y0, W, y1)
        c.itemconfigure(self._pool_bg[s], fill=bg, state="normal")

        c.coords(self._pool_name[s], CX[0] + scaled(4), yc)
        name_max_px = CW[0] - scaled(12)
        name_text = _truncate(row["name"], name_max_px, _tk_font_cache) if _tk_font_cache else row["name"]
        c.itemconfigure(self._pool_name[s], text=name_text, state="normal")

        c.coords(self._pool_modid[s], CX[1] + scaled(4), yc)
        c.itemconfigure(self._pool_modid[s],
                        text=str(row["mod_id"]) if row["mod_id"] else "—",
                        state="normal")

        # Version button (filled accent rectangle)
        vx0 = CX[2] + scaled(4)
        vx1 = CX[2] + CW[2] - scaled(4)
        vy0 = y0 + scaled(3)
        vy1 = y1 - scaled(3)
        c.coords(self._pool_ver_btn[s], vx0, vy0, vx1, vy1)
        c.itemconfigure(self._pool_ver_btn[s], state="normal")

        c.coords(self._pool_ver[s], vx0 + scaled(4), yc)
        c.itemconfigure(self._pool_ver[s], text=row["ver_label"], state="normal")

        # Optional checkbox
        cbx0 = CX[5] + (CW[5] - _CB_SIZE) // 2
        cbx1 = cbx0 + _CB_SIZE
        cby0 = y0 + _CB_PAD
        cby1 = cby0 + _CB_SIZE
        c.coords(self._pool_cb_rect[s], cbx0, cby0, cbx1, cby1)
        c.itemconfigure(self._pool_cb_rect[s], state="normal")
        c.coords(self._pool_cb_mark[s], (cbx0 + cbx1) // 2, (cby0 + cby1) // 2)
        c.itemconfigure(self._pool_cb_mark[s],
                        state="normal" if row["optional"] else "hidden")

        # Fomod checkbox (read-only indicator)
        fmx0 = CX[4] + (CW[4] - _CB_SIZE) // 2
        fmx1 = fmx0 + _CB_SIZE
        fmy0 = y0 + _CB_PAD
        fmy1 = fmy0 + _CB_SIZE
        c.coords(self._pool_fomod_rect[s], fmx0, fmy0, fmx1, fmy1)
        c.itemconfigure(self._pool_fomod_rect[s],
                        state="normal" if row["has_fomod"] else "hidden")
        c.coords(self._pool_fomod_mark[s], (fmx0 + fmx1) // 2, (fmy0 + fmy1) // 2)
        c.itemconfigure(
            self._pool_fomod_mark[s],
            state="normal" if (row["has_fomod"] and row.get("fomod_export", True)) else "hidden",
        )

        # Source button
        src_color, src_text = _source_btn_style(row["source"])
        src_fg = "white"
        sx0 = CX[3] + scaled(4)
        sx1 = CX[3] + CW[3] - scaled(4)
        sy0 = y0 + scaled(3)
        sy1 = y1 - scaled(3)
        c.coords(self._pool_src_btn[s], sx0, sy0, sx1, sy1)
        c.itemconfigure(self._pool_src_btn[s], fill=src_color, state="normal")
        c.coords(self._pool_src_lbl[s], (sx0 + sx1) // 2, yc)
        c.itemconfigure(self._pool_src_lbl[s], text=src_text, fill=src_fg, state="normal")

        self._pool_slot[s] = data_idx

    # ------------------------------------------------------------------
    # Scrolling
    # ------------------------------------------------------------------

    def _scroll(self, units: int):
        self._canvas.yview_scroll(units * _ROW_H, "units")
        self.after_idle(self._redraw)

    def _on_yscroll(self, *args):
        self._sb.set(*args)
        self.after_idle(self._redraw)

    def _update_header(self):
        cw, cx = self._col_cw, self._col_cx
        for i, lbl in enumerate(self._hdr_labels):
            lbl.place(x=cx[i] + scaled(4), y=scaled(2),
                      width=cw[i] - scaled(4), height=scaled(18))

    def _on_canvas_configure(self, event):
        self._canvas_w = event.width
        self._col_cw, self._col_cx = _compute_col_layout(event.width)
        self._update_header()
        # Reposition all visible slots at new column positions without hiding
        for s in range(self._POOL):
            if self._pool_slot[s] != -1:
                self._fill_slot(s, self._pool_slot[s])
        n = len(self._rows)
        if n:
            self._canvas.configure(scrollregion=(0, 0, event.width, n * _ROW_H))
        self.after_idle(self._redraw)

    # ------------------------------------------------------------------
    # Click handling
    # ------------------------------------------------------------------

    def _on_canvas_click(self, event):
        cx = int(self._canvas.canvasx(event.x))
        cy = int(self._canvas.canvasy(event.y))
        data_idx = cy // _ROW_H
        if data_idx < 0 or data_idx >= len(self._rows):
            return

        CW = self._col_cw
        CX = self._col_cx

        # Optional checkbox
        if CX[5] <= cx < CX[5] + CW[5]:
            row = self._rows[data_idx]
            row["optional"] = not row["optional"]
            for s in range(self._POOL):
                if self._pool_slot[s] == data_idx:
                    self._canvas.itemconfigure(
                        self._pool_cb_mark[s],
                        state="normal" if row["optional"] else "hidden",
                    )
                    break
            return

        # Fomod checkbox — only toggleable when a fomod selection exists.
        if CX[4] <= cx < CX[4] + CW[4]:
            row = self._rows[data_idx]
            if row.get("has_fomod"):
                row["fomod_export"] = not row.get("fomod_export", True)
                for s in range(self._POOL):
                    if self._pool_slot[s] == data_idx:
                        self._canvas.itemconfigure(
                            self._pool_fomod_mark[s],
                            state="normal" if row["fomod_export"] else "hidden",
                        )
                        break
            return

        # Version button
        if CX[2] <= cx < CX[2] + CW[2]:
            self._open_version_overlay(data_idx)
            return

        # Source button
        if CX[3] <= cx < CX[3] + CW[3]:
            self._open_source_overlay(data_idx)

    # ------------------------------------------------------------------
    # Version picker overlay
    # ------------------------------------------------------------------

    def _open_version_overlay(self, data_idx: int):
        self._close_version_overlay()
        row = self._rows[data_idx]

        # Kick off fetch if needed
        if not row["versions_fetched"] and row["mod_id"]:
            row["versions_fetched"] = True
            Thread(target=self._fetch_versions, args=(data_idx,), daemon=True).start()

        # Build entry dicts for the picker
        cur_label = row["ver_label"]
        mod_name_norm = _norm_ver_name(row["name"])
        entries = [
            {"label": e["label"], "name": e["name"],
             "current": e["label"] == cur_label,
             "matches": bool(mod_name_norm) and _norm_ver_name(e["name"]) == mod_name_norm}
            for e in row["ver_options"]
        ]

        parent = self._overlay_parent if self._overlay_parent else self
        overlay = VersionPickerOverlay(
            parent,
            mod_name=row["name"],
            entries=entries,
            on_pick=lambda label, di=data_idx: self._set_version(di, label),
            on_close=self._close_version_overlay,
        )
        overlay.place(relx=0, rely=0, relwidth=1, relheight=1)
        self._version_overlay = overlay

    def _close_version_overlay(self):
        if self._version_overlay is not None:
            try:
                self._version_overlay.destroy()
            except Exception:
                pass
            self._version_overlay = None

    def _set_version(self, data_idx: int, label: str):
        row = self._rows[data_idx]
        row["ver_label"] = label
        # Sync size from the chosen option
        opt = next((o for o in row["ver_options"] if o["label"] == label), None)
        if opt:
            row["size_bytes"] = opt.get("size_bytes", 0)
        # Sync file_id parsed from "fileid — version"
        if label and " — " in label:
            try:
                row["file_id"] = int(label.split(" — ")[0])
            except ValueError:
                pass
        for s in range(self._POOL):
            if self._pool_slot[s] == data_idx:
                self._canvas.itemconfigure(self._pool_ver[s], text=label)
                break

    # ------------------------------------------------------------------
    # Source overlay
    # ------------------------------------------------------------------

    def _open_source_overlay(self, data_idx: int):
        self._close_source_overlay()
        self._close_version_overlay()
        row    = self._rows[data_idx]
        parent = self._overlay_parent if self._overlay_parent else self
        overlay = SourcePickerOverlay(
            parent,
            mod_name=row["name"],
            current_source=row["source"],
            current_url=row["direct_url"],
            on_pick=lambda src, url, di=data_idx: self._set_source(di, src, url),
            on_close=self._close_source_overlay,
        )
        overlay.place(relx=0, rely=0, relwidth=1, relheight=1)
        self._source_overlay = overlay

    def _close_source_overlay(self):
        if self._source_overlay is not None:
            try:
                self._source_overlay.destroy()
            except Exception:
                pass
            self._source_overlay = None

    def _set_source(self, data_idx: int, source: str, url: str):
        row = self._rows[data_idx]
        row["source"]     = source
        row["direct_url"] = url
        src_color, src_text = _source_btn_style(source)
        for s in range(self._POOL):
            if self._pool_slot[s] == data_idx:
                self._canvas.itemconfigure(self._pool_src_btn[s], fill=src_color)
                self._canvas.itemconfigure(self._pool_src_lbl[s], text=src_text)
                break

    # ------------------------------------------------------------------
    # Lazy version fetch
    # ------------------------------------------------------------------

    def _fetch_versions(self, data_idx: int):
        row = self._rows[data_idx]
        try:
            result = self._api.get_mod_files(self._game_domain, row["mod_id"])
            files  = result.files if result else []
        except Exception:
            files = []
        self.after(0, lambda: self._apply_versions(data_idx, files))

    def _apply_versions(self, data_idx: int, files):
        row = self._rows[data_idx]
        # Sort latest first by upload timestamp
        sorted_files = sorted(files, key=lambda f: f.uploaded_timestamp, reverse=True)
        options = [
            {
                "label":      f"{f.file_id} — {f.version}",
                "name":       f.name,
                "size_bytes": f.size_in_bytes if f.size_in_bytes is not None else (f.size_kb * 1024),
            }
            for f in sorted_files if f.file_id
        ]
        if not options:
            return
        row["ver_options"] = options
        # Only auto-select if the current label is still the placeholder (user hasn't picked yet).
        # This ensures opening the picker doesn't silently change the file_id.
        cur_label = row["ver_label"]
        is_placeholder = not cur_label or cur_label == "—" or " — " not in cur_label
        if is_placeholder:
            preferred = str(row["file_id"])
            matched   = next((o for o in options if o["label"].startswith(preferred + " —")), None)
            selected  = matched or options[0]
            row["ver_label"]  = selected["label"]
            row["size_bytes"] = selected["size_bytes"]
            try:
                row["file_id"] = int(selected["label"].split(" — ")[0])
            except (ValueError, IndexError):
                pass
            for s in range(self._POOL):
                if self._pool_slot[s] == data_idx:
                    self._canvas.itemconfigure(self._pool_ver[s], text=selected["label"])
                    break
        # Refresh the overlay if it's open for this row
        if (self._version_overlay is not None
                and self._version_overlay.winfo_exists()):
            self._open_version_overlay(data_idx)
