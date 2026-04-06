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
from gui.theme import (
    BG_DEEP,
    BG_HEADER,
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
    scaled,
    font_sized,
)

_ROW_H   = scaled(26)
_HEADERS = ("Mod Name", "Mod ID", "Preferred Version", "Optional", "Fomod")
_CW = (380, 90, 240, 70, 60)
_CX = (0, _CW[0], _CW[0]+_CW[1], _CW[0]+_CW[1]+_CW[2], _CW[0]+_CW[1]+_CW[2]+_CW[3])

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
    # Column widths and x-offsets
    _VCW = (180, 220)   # file-id/version col, name col
    _VCX = (0, 180)

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

        self._font = font_sized("Segoe UI", 10)

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

        hdr = tk.Frame(self, bg=BG_HEADER, height=scaled(22))
        hdr.pack(side="top", fill="x")
        hdr.pack_propagate(False)
        for text, cx in zip(("File ID — Version", "Name"), self._VCX):
            tk.Label(
                hdr, text=text, bg=BG_HEADER, fg=TEXT_MAIN,
                font=FONT_BOLD, anchor="w",
            ).place(x=cx + scaled(4), y=scaled(2), width=self._VCW[0] - scaled(4), height=scaled(18))

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
            lbl = c.create_text(self._VCX[0] + scaled(4), OFF, text="", anchor="w",
                                fill=TEXT_MAIN, font=fn, state="hidden")
            nm  = c.create_text(self._VCX[1] + scaled(4), OFF, text="", anchor="w",
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
                self._apply_row_bg(showing[di], di)
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
            fg = "white" if is_cur else TEXT_MAIN
            fg2 = "white" if is_cur else TEXT_DIM

            c.coords(self._pool_label[s], self._VCX[0] + scaled(4), yc)
            c.itemconfigure(self._pool_label[s], text=entry["label"], fill=fg, state="normal")

            c.coords(self._pool_name[s], self._VCX[1] + scaled(4), yc)
            c.itemconfigure(self._pool_name[s], text=entry["name"], fill=fg2, state="normal")

    def _apply_row_bg(self, s: int, di: int):
        entry  = self._ver_entries[di]
        is_cur = entry["label"] == self._current
        if is_cur:
            fill = ACCENT
        elif di == self._hover_idx:
            fill = BG_HOVER
        else:
            fill = BG_ROW if di % 2 == 0 else BG_DEEP
        self._canvas.itemconfigure(self._pool_bg[s], fill=fill)
        fg  = "white" if is_cur else TEXT_MAIN
        fg2 = "white" if is_cur else TEXT_DIM
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

    def _on_canvas_configure(self, event):
        self._canvas_w = event.width
        n = len(self._ver_entries)
        if n:
            self._canvas.configure(scrollregion=(0, 0, event.width, n * self._ROW_H))
        self.after_idle(self._redraw)


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

        self._pool_bg:      list[int] = []
        self._pool_name:    list[int] = []
        self._pool_modid:   list[int] = []
        self._pool_ver:     list[int] = []
        self._pool_ver_btn: list[int] = []
        self._pool_cb_rect:    list[int] = []
        self._pool_cb_mark:    list[int] = []
        self._pool_fomod_rect: list[int] = []
        self._pool_fomod_mark: list[int] = []
        self._pool_slot:       list[int] = []

        self._canvas_w  = 780
        self._font_main = font_sized("Segoe UI", 10)
        self._font_small = font_sized("Segoe UI", 9)

        global _tk_font_cache
        if _tk_font_cache is None:
            _tk_font_cache = tkfont.Font(family="Segoe UI", size=10)

        self._version_overlay: Optional[VersionPickerOverlay] = None

        self._build_ui()
        self._create_pool()
        self.after(50, self._load_mods)

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------

    def _build_ui(self):
        self.grid_rowconfigure(2, weight=1)
        self.grid_rowconfigure(0, weight=0)
        self.grid_rowconfigure(1, weight=0)
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

        tk.Label(
            toolbar, text="Workshop", bg=BG_HEADER, fg=TEXT_MAIN,
            font=FONT_BOLD,
        ).pack(side="left", padx=8)

        hdr = tk.Frame(self, bg=BG_HEADER, height=scaled(22))
        hdr.grid(row=1, column=0, sticky="ew")
        hdr.grid_propagate(False)
        for text, cx, cw in zip(_HEADERS, _CX, _CW):
            tk.Label(
                hdr, text=text, bg=BG_HEADER, fg=TEXT_MAIN,
                font=FONT_BOLD, anchor="w",
            ).place(x=cx + scaled(4), y=scaled(2), width=cw - scaled(4), height=scaled(18))

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
        if self._on_close:
            self._on_close()
        else:
            self.destroy()

    def _do_export(self):
        if not self._rows:
            messagebox.showwarning("Workshop", "No mods to export.", parent=self)
            return

        pick_save_file(
            "Export Amethyst Manifest",
            lambda p: self.after(0, lambda: self._prefetch_sizes_then_export(p)),
            current_name="manifest.json",
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
            row for row in self._rows
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
        for row in self._rows:
            # Parse fileid from ver_label (format "fileid — version" or just "—")
            ver_label = row["ver_label"]
            file_id   = row["file_id"]
            if ver_label and " — " in ver_label:
                try:
                    file_id = int(ver_label.split(" — ")[0])
                except ValueError:
                    pass

            source: dict = {
                "modId":  row["mod_id"],
                "fileId": file_id,
            }
            if row.get("size_bytes"):
                source["fileSize"] = row["size_bytes"]

            mod_entry: dict = {
                "name":     row["name"],
                "source":   source,
                "optional": row["optional"],
            }

            if row["has_fomod"] and game_name:
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

        load_order = [r["name"] for r in self._rows]

        plugins: list[dict] = []
        if profile_dir:
            plugins_path = Path(profile_dir) / "plugins.txt"
            if plugins_path.is_file():
                try:
                    for p in read_plugins(plugins_path):
                        plugins.append({"name": p.name, "enabled": p.enabled})
                except Exception:
                    pass

        manifest = {
            "AmethystManifest": True,
            "mods":             mods,
            "loadOrder":        load_order,
            "plugins":          plugins,
        }

        try:
            out_path = Path(out_path)
            if out_path.suffix.lower() != ".json":
                out_path = out_path.with_suffix(".json")
            with open(out_path, "w", encoding="utf-8") as fh:
                json.dump(manifest, fh, indent=2)
            messagebox.showinfo("Workshop", f"Manifest exported to:\n{out_path}", parent=self)
        except Exception as exc:
            messagebox.showerror("Workshop", f"Export failed:\n{exc}", parent=self)

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

            self._pool_bg.append(bg)
            self._pool_name.append(name)
            self._pool_modid.append(mid)
            self._pool_ver_btn.append(vbtn)
            self._pool_ver.append(ver)
            self._pool_cb_rect.append(cbr)
            self._pool_cb_mark.append(cbm)
            self._pool_fomod_rect.append(fmr)
            self._pool_fomod_mark.append(fmm)
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
            if staging_root:
                meta_path = staging_root / entry.name / "meta.ini"
                if meta_path.is_file():
                    try:
                        meta    = read_meta(meta_path)
                        mod_id  = meta.mod_id  or 0
                        file_id = meta.file_id or 0
                        version = meta.version or ""
                    except Exception:
                        pass

            if file_id and version:
                ver_label = f"{file_id} — {version}"
            elif file_id:
                ver_label = str(file_id)
            else:
                ver_label = "—"

            has_fomod = (
                game_name is not None
                and get_fomod_selections_path(game_name, entry.name).is_file()
            )

            self._rows.append({
                "name":             entry.name,
                "mod_id":           mod_id,
                "file_id":          file_id,
                "ver_label":        ver_label,
                "ver_options":      [{"label": ver_label, "name": "", "size_bytes": 0}],
                "optional":         False,
                "has_fomod":        has_fomod,
                "versions_fetched": False,
                "size_bytes":       0,
            })

        n = len(self._rows)
        self._canvas.configure(scrollregion=(0, 0, self._canvas_w, n * _ROW_H))
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
                    self._pool_fomod_rect, self._pool_fomod_mark):
            c.itemconfigure(lst[s], state="hidden")
        self._pool_slot[s] = -1

    def _fill_slot(self, s: int, data_idx: int):
        row = self._rows[data_idx]
        c   = self._canvas
        y0  = data_idx * _ROW_H
        y1  = y0 + _ROW_H
        yc  = y0 + _ROW_H // 2
        W   = self._canvas_w
        bg  = BG_ROW if data_idx % 2 == 0 else BG_DEEP

        c.coords(self._pool_bg[s], 0, y0, W, y1)
        c.itemconfigure(self._pool_bg[s], fill=bg, state="normal")

        c.coords(self._pool_name[s], _CX[0] + scaled(4), yc)
        name_max_px = _CW[0] - scaled(12)
        name_text = _truncate(row["name"], name_max_px, _tk_font_cache) if _tk_font_cache else row["name"]
        c.itemconfigure(self._pool_name[s], text=name_text, state="normal")

        c.coords(self._pool_modid[s], _CX[1] + scaled(4), yc)
        c.itemconfigure(self._pool_modid[s],
                        text=str(row["mod_id"]) if row["mod_id"] else "—",
                        state="normal")

        # Version button (filled accent rectangle)
        vx0 = _CX[2] + scaled(4)
        vx1 = _CX[2] + _CW[2] - scaled(4)
        vy0 = y0 + scaled(3)
        vy1 = y1 - scaled(3)
        c.coords(self._pool_ver_btn[s], vx0, vy0, vx1, vy1)
        c.itemconfigure(self._pool_ver_btn[s], state="normal")

        c.coords(self._pool_ver[s], vx0 + scaled(4), yc)
        c.itemconfigure(self._pool_ver[s], text=row["ver_label"], state="normal")

        # Optional checkbox
        cbx0 = _CX[3] + (_CW[3] - _CB_SIZE) // 2
        cbx1 = cbx0 + _CB_SIZE
        cby0 = y0 + _CB_PAD
        cby1 = cby0 + _CB_SIZE
        c.coords(self._pool_cb_rect[s], cbx0, cby0, cbx1, cby1)
        c.itemconfigure(self._pool_cb_rect[s], state="normal")
        c.coords(self._pool_cb_mark[s], (cbx0 + cbx1) // 2, (cby0 + cby1) // 2)
        c.itemconfigure(self._pool_cb_mark[s],
                        state="normal" if row["optional"] else "hidden")

        # Fomod checkbox (read-only indicator)
        fmx0 = _CX[4] + (_CW[4] - _CB_SIZE) // 2
        fmx1 = fmx0 + _CB_SIZE
        fmy0 = y0 + _CB_PAD
        fmy1 = fmy0 + _CB_SIZE
        c.coords(self._pool_fomod_rect[s], fmx0, fmy0, fmx1, fmy1)
        c.itemconfigure(self._pool_fomod_rect[s],
                        state="normal" if row["has_fomod"] else "hidden")
        c.coords(self._pool_fomod_mark[s], (fmx0 + fmx1) // 2, (fmy0 + fmy1) // 2)
        c.itemconfigure(self._pool_fomod_mark[s],
                        state="normal" if row["has_fomod"] else "hidden")

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

    def _on_canvas_configure(self, event):
        self._canvas_w = event.width
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

        # Optional checkbox
        if _CX[3] <= cx < _CX[3] + _CW[3]:
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

        # Version button
        if _CX[2] <= cx < _CX[2] + _CW[2]:
            self._open_version_overlay(data_idx)

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
        entries = [
            {"label": e["label"], "name": e["name"],
             "current": e["label"] == cur_label}
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
        for s in range(self._POOL):
            if self._pool_slot[s] == data_idx:
                self._canvas.itemconfigure(self._pool_ver[s], text=label)
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
            for s in range(self._POOL):
                if self._pool_slot[s] == data_idx:
                    self._canvas.itemconfigure(self._pool_ver[s], text=selected["label"])
                    break
        # Refresh the overlay if it's open for this row
        if (self._version_overlay is not None
                and self._version_overlay.winfo_exists()):
            self._open_version_overlay(data_idx)
