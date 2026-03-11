"""
collections_dialog.py
Browse Nexus Mods Collections for the currently selected game via GraphQL.

Opens as a standalone Toplevel window.  Displays 20 collections per page,
sorted by most downloaded by default.  Includes a search bar to filter
by name, and Prev / Next page navigation.
"""

from __future__ import annotations

import re
import threading
import tkinter as tk
import tkinter.ttk as ttk
import webbrowser
from pathlib import Path
from typing import Callable, Optional

import customtkinter as ctk
from PIL import Image

from gui.game_helpers import _create_profile, _profiles_for_game
from gui.install_mod import install_mod_from_archive
from gui.mod_card import CARD_PAD, make_placeholder_image
from gui.mod_name_utils import _suggest_mod_names
from Utils.modlist import write_modlist, read_modlist, ModEntry
from Nexus.nexus_meta import build_meta_from_download

# Collections-specific card dimensions (5-column grid)
_COLL_COLS  = 5
_COLL_W     = 200
_COLL_IMG_W = 190
_COLL_IMG_H = 240
from gui.theme import (
    BG_DEEP,
    BG_PANEL,
    BG_HEADER,
    BG_ROW,
    BG_SEP,
    ACCENT,
    ACCENT_HOV,
    TEXT_MAIN,
    TEXT_DIM,
    BORDER,
    FONT_NORMAL,
    FONT_BOLD,
    FONT_SMALL,
)

PAGE_SIZE    = 20
_SUMMARY_MAX = 200


def _fmt_size(n_bytes: int) -> str:
    """Human-readable file size."""
    if n_bytes <= 0:
        return "—"
    for unit, threshold in (("GB", 1 << 30), ("MB", 1 << 20), ("KB", 1 << 10)):
        if n_bytes >= threshold:
            return f"{n_bytes / threshold:.1f} {unit}"
    return f"{n_bytes} B"


def _fomod_choices_from_collection(choices: dict) -> "dict[str, dict[str, list[str]]]":
    """Convert a collection.json FOMOD choices block to the saved_selections
    format that ``resolve_files()`` / ``FomodDialog`` expect.

    Collection format::

        {
          "type": "fomod",
          "options": [
            {
              "name": "<step_name>",
              "groups": [
                {
                  "name": "<group_name>",
                  "choices": [{"name": "<plugin_name>", "idx": 0}, ...]
                },
                ...
              ]
            },
            ...
          ]
        }

    Saved-selections format::

        {
          "<step_name>": {
            "<group_name>": ["<plugin_name>", ...]
          },
          ...
        }
    """
    result: dict = {}
    for step in choices.get("options", []):
        step_name = step.get("name", "")
        groups: dict = {}
        for group in step.get("groups", []):
            group_name = group.get("name", "")
            plugin_names = [c["name"] for c in group.get("choices", []) if c.get("name")]
            if plugin_names:
                groups[group_name] = plugin_names
        if groups:
            result[step_name] = groups
    return result


# ---------------------------------------------------------------------------
# CollectionCard widget
# ---------------------------------------------------------------------------

class CollectionCard:
    """A card widget that displays a single Nexus Mods collection."""

    def __init__(self, parent: tk.Widget, collection, on_view: Callable):
        self._collection = collection
        self._img_label: Optional[ctk.CTkLabel] = None

        # Outer card frame — fixed size, content clips if too long.
        self.card = tk.Frame(
            parent,
            width=_COLL_W, height=480,
            bg=BG_PANEL,
            highlightbackground=BORDER,
            highlightthickness=1,
        )
        self.card.pack_propagate(False)
        self.card.grid_propagate(False)

        self._build(on_view)

    def _build(self, on_view: Callable):
        col = self._collection

        # Tile image placeholder
        placeholder = make_placeholder_image(_COLL_IMG_W, _COLL_IMG_H)
        ph_ctk = ctk.CTkImage(light_image=placeholder, dark_image=placeholder,
                               size=(_COLL_IMG_W, _COLL_IMG_H))
        self._img_label = ctk.CTkLabel(
            self.card, image=ph_ctk, text="",
            width=_COLL_IMG_W, height=_COLL_IMG_H,
        )
        self._img_label.pack(padx=5, pady=(6, 3))

        # Button row — fixed-height footer so all cards align consistently (packed first so it's anchored to the bottom)
        btn_frame = tk.Frame(self.card, bg=BG_PANEL, height=44)
        btn_frame.pack(side="bottom", fill="x")
        btn_frame.pack_propagate(False)
        ctk.CTkButton(
            btn_frame, text="View",
            width=_COLL_W - 20, height=28,
            fg_color=ACCENT, hover_color=ACCENT_HOV,
            text_color="#ffffff", font=FONT_SMALL,
            command=on_view,
        ).place(relx=0.5, rely=0.5, anchor="center")

        # Text area — fills width, grows to fit its content
        text_frame = tk.Frame(self.card, bg=BG_PANEL)
        text_frame.pack(fill="x")

        # Name
        name_text = col.name or f"Collection {col.id}"
        ctk.CTkLabel(
            text_frame, text=name_text,
            font=FONT_BOLD, text_color=TEXT_MAIN,
            wraplength=_COLL_W - 16, justify="left", anchor="w",
        ).pack(padx=8, fill="x")

        # Stats: downloads, endorsements, mod count
        stats = f"↓{col.total_downloads:,}  ♥{col.endorsements:,}  {col.mod_count} mods"
        ctk.CTkLabel(
            text_frame, text=stats,
            font=FONT_SMALL, text_color=TEXT_DIM,
            anchor="w",
        ).pack(padx=8, fill="x")

        # Author
        if col.user_name:
            ctk.CTkLabel(
                text_frame, text=f"by {col.user_name}",
                font=FONT_SMALL, text_color=TEXT_DIM,
                anchor="w",
            ).pack(padx=8, fill="x")

        # Summary
        summary = (col.summary or "").strip()
        if len(summary) > _SUMMARY_MAX:
            summary = summary[:_SUMMARY_MAX].rstrip() + "…"
        if summary:
            ctk.CTkLabel(
                text_frame, text=summary,
                font=FONT_SMALL, text_color=TEXT_DIM,
                wraplength=_COLL_W - 16, justify="left", anchor="w",
            ).pack(padx=8, pady=(2, 0), fill="x")



    def load_image_async(self, url: str, cache: dict, loading: set, root: tk.Widget):
        """Start async tile image load (same pattern as mod_card.py)."""
        if not url:
            return
        if url in cache:
            self._apply_image(cache[url])
            return
        if url in loading:
            return
        loading.add(url)

        def _fetch():
            try:
                import requests
                r = requests.get(url, timeout=10)
                r.raise_for_status()
                from io import BytesIO
                raw = Image.open(BytesIO(r.content)).convert("RGBA")
                # Scale to cover the slot (zoom), then center-crop
                src_w, src_h = raw.size
                scale = max(_COLL_IMG_W / src_w, _COLL_IMG_H / src_h)
                new_w = int(src_w * scale)
                new_h = int(src_h * scale)
                raw = raw.resize((new_w, new_h), Image.LANCZOS)
                x_off = (new_w - _COLL_IMG_W) // 2
                y_off = (new_h - _COLL_IMG_H) // 2
                bg = raw.crop((x_off, y_off, x_off + _COLL_IMG_W, y_off + _COLL_IMG_H))
                photo = ctk.CTkImage(light_image=bg, dark_image=bg,
                                     size=(_COLL_IMG_W, _COLL_IMG_H))
                cache[url] = photo
                root.after(0, lambda: self._apply_image(photo))
            except Exception:
                pass
            finally:
                loading.discard(url)

        threading.Thread(target=_fetch, daemon=True).start()

    def _apply_image(self, photo: ctk.CTkImage):
        try:
            if self._img_label and self._img_label.winfo_exists():
                self._img_label.configure(image=photo)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# _OptionalModsDialog
# ---------------------------------------------------------------------------

class _OptionalModsDialog(ctk.CTkToplevel):
    """Modal dialog that lists optional mods with checkboxes (all checked by default).

    Show before installing a collection so the user can deselect mods they
    do not want.  After ``wait_window()`` inspect:

    * ``dialog.result is None``  → user cancelled; abort the install.
    * ``dialog.result`` (set)    → ``file_id`` values of optional mods to **skip**.
    """

    _ROW_H = 30

    def __init__(self, parent, optional_mods: list):
        super().__init__(parent)
        self.title("Optional Mods")
        self.resizable(True, True)
        self.configure(fg_color=BG_DEEP)

        self.result = None          # None = cancelled; set = file_ids to skip
        self._optional_mods = optional_mods
        self._vars: dict[int, tk.BooleanVar] = {}  # file_id → BooleanVar (True = include)

        self._build_ui()

        # Size & centre on parent.
        # Use the actual requisition height so nothing is clipped.
        self.update_idletasks()
        w  = 540
        h  = self.winfo_reqheight()
        px = parent.winfo_rootx() + (parent.winfo_width()  - w) // 2
        py = parent.winfo_rooty() + (parent.winfo_height() - h) // 2
        self.geometry(f"{w}x{h}+{px}+{py}")
        self.minsize(380, 220)

        self.grab_set()
        self.focus_force()
        self.protocol("WM_DELETE_WINDOW", self._on_cancel)

    # ------------------------------------------------------------------
    def _build_ui(self):
        _MAX_LIST_H = 400

        # --- Header ---
        hdr = tk.Frame(self, bg=BG_HEADER, pady=6, bd=0, highlightthickness=0)
        hdr.pack(fill="x", side="top")
        tk.Label(
            hdr, text="Optional Mods",
            bg=BG_HEADER, fg=TEXT_MAIN,
            font=("Segoe UI", 12, "bold"), anchor="w",
        ).pack(side="left", padx=14)

        # --- Subtitle ---
        tk.Label(
            self,
            text=(f"{len(self._optional_mods)} optional mod(s) found. "
                  "Uncheck any you do not want installed:"),
            bg=BG_DEEP, fg=TEXT_DIM,
            font=FONT_SMALL, anchor="w",
        ).pack(fill="x", padx=12, pady=(6, 2))

        # --- Footer (packed BEFORE the expanding list so it is never hidden) ---
        ftr = tk.Frame(self, bg=BG_HEADER, pady=0, bd=0, highlightthickness=0)
        ftr.pack(fill="x", side="bottom")

        ctk.CTkButton(
            ftr, text="Cancel",
            height=30, fg_color="#3c3c3c", hover_color="#505050",
            text_color=TEXT_MAIN, font=("Segoe UI", 10),
            border_width=0,
            command=self._on_cancel,
        ).pack(side="right", padx=10, pady=8)

        ctk.CTkButton(
            ftr, text="Install",
            height=30, fg_color="#2d7a2d", hover_color="#3a9e3a",
            text_color="#ffffff", font=("Segoe UI", 10, "bold"),
            border_width=0,
            command=self._on_ok,
        ).pack(side="right", padx=(0, 4), pady=8)

        ctk.CTkButton(
            ftr, text="Deselect All",
            height=30, fg_color=BG_PANEL, hover_color=BG_HEADER,
            text_color=TEXT_DIM, font=("Segoe UI", 10),
            border_width=0,
            command=self._deselect_all,
        ).pack(side="left", padx=(4, 0), pady=8)

        ctk.CTkButton(
            ftr, text="Select All",
            height=30, fg_color=BG_PANEL, hover_color=BG_HEADER,
            text_color=TEXT_DIM, font=("Segoe UI", 10),
            border_width=0,
            command=self._select_all,
        ).pack(side="left", padx=(10, 4), pady=8)

        # --- Scrollable list (packed last so it fills whatever remains) ---
        list_frame = tk.Frame(self, bg=BG_DEEP, bd=0, highlightthickness=0)
        list_frame.pack(fill="both", expand=True, padx=8, pady=(0, 4))

        # Give the canvas an explicit height so winfo_reqheight() is meaningful
        canvas_h = min(len(self._optional_mods) * self._ROW_H + 4, _MAX_LIST_H)
        canvas = tk.Canvas(
            list_frame, bg=BG_DEEP, bd=0, highlightthickness=0,
            yscrollincrement=1, height=canvas_h,
        )
        vsb = tk.Scrollbar(
            list_frame, orient="vertical", command=canvas.yview,
            bg=BG_SEP, troughcolor=BG_DEEP, activebackground=ACCENT,
            highlightthickness=0, bd=0,
        )
        canvas.configure(yscrollcommand=vsb.set)

        inner = tk.Frame(canvas, bg=BG_DEEP, bd=0, highlightthickness=0)
        canvas_window = canvas.create_window((0, 0), window=inner, anchor="nw")

        def _on_inner_resize(_e):
            canvas.configure(scrollregion=canvas.bbox("all"))
            canvas.itemconfigure(canvas_window, width=canvas.winfo_width())

        def _on_canvas_resize(e):
            canvas.itemconfigure(canvas_window, width=e.width)

        inner.bind("<Configure>", _on_inner_resize)
        canvas.bind("<Configure>", _on_canvas_resize)

        # Scroll-wheel helpers (Linux Button-4/5 + Windows/macOS MouseWheel)
        def _scroll_up(_e):   canvas.yview_scroll(-3, "units")
        def _scroll_down(_e): canvas.yview_scroll(3, "units")
        def _on_wheel(e):
            canvas.yview_scroll(-3 if getattr(e, "delta", 0) > 0 else 3, "units")

        def _bind_scroll(w):
            w.bind("<Button-4>",   _scroll_up)
            w.bind("<Button-5>",   _scroll_down)
            w.bind("<MouseWheel>", _on_wheel)

        for w in (canvas, vsb, inner, list_frame, self):
            _bind_scroll(w)

        canvas.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")

        # --- Rows ---
        for i, mod in enumerate(self._optional_mods):
            var = tk.BooleanVar(value=True)
            self._vars[mod.file_id] = var

            row_bg = BG_ROW if i % 2 == 0 else BG_PANEL
            row = tk.Frame(inner, bg=row_bg, bd=0, highlightthickness=0,
                           height=self._ROW_H)
            row.pack(fill="x")
            row.pack_propagate(False)

            cb = tk.Checkbutton(
                row, variable=var,
                bg=row_bg, fg=TEXT_MAIN,
                activebackground=row_bg, activeforeground=TEXT_MAIN,
                selectcolor=BG_DEEP,
                bd=0, highlightthickness=0, cursor="hand2",
            )
            cb.pack(side="left", padx=(8, 0))

            name_text = mod.mod_name or mod.file_name or "(Unknown)"
            tk.Label(
                row, text=name_text,
                bg=row_bg, fg=TEXT_MAIN,
                font=FONT_NORMAL, anchor="w",
            ).pack(side="left", padx=(4, 4))

            if mod.mod_author:
                tk.Label(
                    row, text=f"by {mod.mod_author}",
                    bg=row_bg, fg=TEXT_DIM,
                    font=FONT_SMALL, anchor="w",
                ).pack(side="left", padx=(0, 8))

            # Bind scrollwheel to every child so it works anywhere in the row
            _bind_scroll(row)
            for child in row.winfo_children():
                _bind_scroll(child)

    # ------------------------------------------------------------------
    def _on_ok(self):
        # file_ids whose checkbox was unchecked → skip them
        self.result = {fid for fid, var in self._vars.items() if not var.get()}
        self.grab_release()
        self.destroy()

    def _on_cancel(self):
        self.result = None
        self.grab_release()
        self.destroy()

    def _select_all(self):
        for var in self._vars.values():
            var.set(True)

    def _deselect_all(self):
        for var in self._vars.values():
            var.set(False)


# ---------------------------------------------------------------------------
# CollectionDetailDialog
# ---------------------------------------------------------------------------

class CollectionDetailDialog(tk.Frame):
    """
    Shows every mod in a collection with file sizes, plus a total size header
    and an Install Collection button. Displayed as an inline overlay frame.
    """

    _TV_COLS = ("Order", "Mod Name", "Author", "File", "Size", "Opt")
    _TV_WIDTHS = (50, 250, 120, 200, 80, 40)

    def __init__(self, parent, collection, game_domain: str, api, game=None, app_root=None, log_fn=None, on_close=None):
        super().__init__(parent, bg=BG_DEEP)
        self._collection = collection
        self._game_domain = game_domain
        self._api = api
        self._game = game
        self._app_root = app_root
        self._log = log_fn or (lambda *a: None)
        self._on_close = on_close or self.destroy

        self._size_var = tk.StringVar(value="Loading\u2026")
        self._status_var = tk.StringVar(value="Fetching mod list\u2026")
        self._loaded_mods: list = []
        self._download_link_path: str = ""
        self._schema_order: dict = {}

        self._reset_btn = None  # created in _build_ui; shown only when profile exists
        self._file_id_to_tree_iid: dict[int, str] = {}  # populated by _populate; used to green rows live

        self._build_ui()
        self._fetch()
        self.after(100, self._update_reset_btn_visibility)  # check after widget is placed

    # ------------------------------------------------------------------
    def _build_ui(self):
        col = self._collection

        # --- Header bar ---
        hdr = tk.Frame(self, bg=BG_HEADER, pady=8, bd=0, highlightthickness=0)
        hdr.pack(fill="x", side="top")

        tk.Label(
            hdr, text=col.name,
            bg=BG_HEADER, fg=TEXT_MAIN,
            font=("Segoe UI", 13, "bold"),
            anchor="w",
        ).pack(side="left", padx=14)

        tk.Label(
            hdr, textvariable=self._size_var,
            bg=BG_HEADER, fg=TEXT_DIM,
            font=("Segoe UI", 10),
            anchor="e",
        ).pack(side="right", padx=14)

        # --- Status bar ---
        self._status_lbl = tk.Label(
            self, textvariable=self._status_var,
            bg=BG_DEEP, fg=TEXT_DIM,
            font=("Segoe UI", 9),
            anchor="w", bd=0, highlightthickness=0,
        )
        self._status_lbl.pack(fill="x", side="top", padx=10, pady=(4, 0))

        # --- Treeview with scrollbars ---
        tree_frame = tk.Frame(self, bg=BG_DEEP, bd=0, highlightthickness=0)
        tree_frame.pack(fill="both", expand=True, padx=8, pady=6)

        vsb = tk.Scrollbar(
            tree_frame, orient="vertical",
            bg=BG_SEP, troughcolor=BG_DEEP, activebackground=ACCENT,
            highlightthickness=0, bd=0,
        )
        hsb = tk.Scrollbar(
            tree_frame, orient="horizontal",
            bg=BG_SEP, troughcolor=BG_DEEP, activebackground=ACCENT,
            highlightthickness=0, bd=0,
        )

        # Style the treeview to match the dark theme.
        # Do NOT call theme_use() here — it changes the global ttk theme and
        # breaks every other ttk widget in the application.
        style = ttk.Style()
        style.configure(
            "CollDetail.Treeview",
            background=BG_PANEL, foreground=TEXT_MAIN,
            fieldbackground=BG_PANEL, rowheight=24,
            font=("Segoe UI", 9),
            borderwidth=0, relief="flat",
        )
        style.configure(
            "CollDetail.Treeview.Heading",
            background=BG_HEADER, foreground=TEXT_MAIN,
            font=("Segoe UI", 9, "bold"),
            borderwidth=0, relief="flat",
        )
        style.map(
            "CollDetail.Treeview",
            background=[("selected", ACCENT)],
            foreground=[("selected", "#ffffff")],
        )
        try:
            style.layout("CollDetail.Treeview", [(
                "CollDetail.Treeview.treearea", {"sticky": "nswe"}
            )])
        except Exception:
            pass  # layout element may differ by theme; harmless to skip

        self._tree = ttk.Treeview(
            tree_frame,
            style="CollDetail.Treeview",
            columns=self._TV_COLS,
            show="headings",
            yscrollcommand=vsb.set,
            xscrollcommand=hsb.set,
            selectmode="browse",
        )
        vsb.config(command=self._tree.yview)
        hsb.config(command=self._tree.xview)

        vsb.pack(side="right", fill="y")
        hsb.pack(side="bottom", fill="x")
        self._tree.pack(fill="both", expand=True)

        # Column headings + widths
        for col_id, width in zip(self._TV_COLS, self._TV_WIDTHS):
            anchor = "center" if col_id == "Order" else "w"
            self._tree.heading(col_id, text=col_id, anchor=anchor)
            stretch = col_id in ("Mod Name", "File")
            self._tree.column(col_id, width=width, minwidth=30, anchor=anchor, stretch=stretch)

        self._tree.tag_configure("odd", background=BG_ROW)
        self._tree.tag_configure("even", background=BG_PANEL)
        self._tree.tag_configure("unordered", foreground="#888888")
        self._tree.tag_configure("installed", background="#1e4d1e")

        # --- Priority note ---
        note = tk.Label(
            self, text="Order = author's install order  (↓ installed last = highest priority)",
            bg=BG_DEEP, fg=TEXT_DIM, font=("Segoe UI", 8), anchor="w",
        )
        note.pack(fill="x", side="top", padx=10, pady=(0, 2))

        # --- Footer ---
        ftr = tk.Frame(self, bg=BG_HEADER, pady=8, bd=0, highlightthickness=0)
        ftr.pack(fill="x", side="bottom")

        ctk.CTkButton(
            ftr, text="Close",
            height=30, fg_color="#3c3c3c", hover_color="#505050",
            text_color=TEXT_MAIN, font=("Segoe UI", 10),
            border_width=0,
            command=self._on_close,
        ).pack(side="right", padx=10, pady=6)

        ctk.CTkButton(
            ftr, text="Install Collection",
            height=30, fg_color="#2d7a2d", hover_color="#3a9e3a",
            text_color="#ffffff", font=("Segoe UI", 10, "bold"),
            border_width=0,
            command=self._on_install_collection,
        ).pack(side="right", padx=(10, 0), pady=6)

        self._reset_btn = ctk.CTkButton(
            ftr, text="Reset Load Order",
            height=30, fg_color="#5a3a00", hover_color="#7a5200",
            text_color="#ffffff", font=("Segoe UI", 10),
            border_width=0,
            command=self._on_reset_load_order,
        )
        # Packed (shown) only when the collection profile already exists;
        # see _update_reset_btn_visibility()

    # ------------------------------------------------------------------
    # Mod-list fetch
    # ------------------------------------------------------------------
    def _fetch(self):
        threading.Thread(target=self._worker, daemon=True).start()

    def _worker(self):
        try:
            name, total_size, mod_count, mods, dl_path = self._api.get_collection_detail(
                self._collection.slug, self._game_domain
            )
            if not name and not mods:
                try:
                    self.after(0, lambda: self._status_var.set(
                        "No data returned — check your Nexus API key or app log."
                    ))
                except Exception:
                    pass
                return

            # Also fetch collection.json to get the authoritative install order
            schema_order: dict[int, int] = {}  # file_id → 0-based position
            cj: dict = {}
            if dl_path:
                try:
                    self.after(0, lambda: self._status_var.set(
                        "Fetching author\'s load order from collection archive…"
                    ))
                    cj = self._api.get_collection_archive_json(dl_path)
                    for pos, m in enumerate(cj.get("mods", [])):
                        fid = (m.get("source") or {}).get("fileId")
                        if fid is not None:
                            schema_order[int(fid)] = pos
                except Exception as exc:
                    self._log(f"CollectionDetail: could not fetch collection.json: {exc}")

            # Cache the full schema dict so _run_install can reuse it without
            # downloading the archive a second time.
            self._collection_schema_cache = cj

            try:
                self.after(0, lambda: self._populate(total_size, mod_count, mods, dl_path, schema_order))
            except Exception:
                pass
        except Exception as exc:
            self._log(f"CollectionDetail error: {exc}")
            try:
                self.after(0, lambda: self._status_var.set(f"Error: {exc}"))
            except Exception:
                pass

    def _populate(self, total_size: int, mod_count: int, mods, dl_path: str = "", schema_order=None):
        schema_order = schema_order or {}
        self._size_var.set(f"Total size: {_fmt_size(total_size)}  |  {mod_count:,} mods")
        self._loaded_mods = mods
        self._download_link_path = dl_path
        self._schema_order = schema_order

        _NO_POS = len(schema_order) + 1
        sorted_mods = sorted(mods, key=lambda m: schema_order.get(m.file_id, _NO_POS))

        has_order = bool(schema_order)
        ordered_count = sum(1 for m in sorted_mods if m.file_id in schema_order)
        if has_order:
            extra = (
                f" ({ordered_count} positioned, {len(mods) - ordered_count} unpositioned)"
                if ordered_count < len(mods) else ""
            )
            self._status_var.set(f"{len(mods):,} mods \u2014 sorted by author's install order{extra}")
        else:
            self._status_var.set(f"{len(mods):,} mod file entries loaded (collection order unavailable)")

        installed_names, file_id_to_folder = self._get_installed_mod_info()
        self._file_id_to_tree_iid.clear()

        for display_i, mod in enumerate(sorted_mods, start=1):
            tag = "odd" if display_i % 2 else "even"
            opt_mark = "\u2713" if mod.optional else ""
            if has_order and mod.file_id in schema_order:
                order_label = str(schema_order[mod.file_id] + 1)
            elif has_order:
                order_label = "\u2014"
                tag = "unordered"
            else:
                order_label = str(display_i)

            # Highlight rows where the mod is already installed in the collection profile
            is_installed = False
            if installed_names is not None:
                if mod.file_id and mod.file_id in file_id_to_folder:
                    is_installed = True
                elif installed_names:
                    for raw in (mod.mod_name or "", mod.file_name or ""):
                        if raw:
                            for s in _suggest_mod_names(raw):
                                if s and s.lower() in installed_names:
                                    is_installed = True
                                    break
                        if is_installed:
                            break

            if is_installed:
                row_tags = ("installed", "unordered") if tag == "unordered" else ("installed",)
            else:
                row_tags = (tag,)

            iid = self._tree.insert(
                "", "end",
                values=(order_label, mod.mod_name, mod.mod_author, mod.file_name,
                        _fmt_size(mod.size_bytes), opt_mark),
                tags=row_tags,
            )
            if mod.file_id:
                self._file_id_to_tree_iid[mod.file_id] = iid

    def _mark_row_installed(self, file_id: int) -> None:
        """Switch a treeview row to the green 'installed' tag (called on main thread)."""
        iid = self._file_id_to_tree_iid.get(file_id)
        if not iid:
            return
        try:
            if not self._tree.exists(iid):
                return
            current_tags = self._tree.item(iid, "tags")
            new_tags = tuple(
                t for t in current_tags if t not in ("odd", "even")
            )
            if "installed" not in new_tags:
                new_tags = ("installed",) + new_tags
            self._tree.item(iid, tags=new_tags)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Collection install
    # ------------------------------------------------------------------
    def _on_install_collection(self):
        """Validate prerequisites then kick off the background install."""
        if not self._game:
            self._status_var.set("Error: no game object — cannot install.")
            return

        app = self._app_root
        downloader = getattr(app, "_nexus_downloader", None)
        if not downloader:
            self._status_var.set("Error: Nexus downloader not available.")
            return

        mods = getattr(self, "_loaded_mods", None)
        if not mods:
            self._status_var.set("Mod list not loaded yet — please wait.")
            return

        # --- Optional mods selection dialog ---
        optional_mods = [m for m in mods if m.optional]
        if optional_mods:
            dlg = _OptionalModsDialog(self.winfo_toplevel(), optional_mods)
            self.wait_window(dlg)
            if dlg.result is None:
                # User cancelled — abort the install
                return
            if dlg.result:
                # dlg.result is the set of file_ids to skip
                skip_ids = dlg.result
                mods = [m for m in mods if not m.optional or m.file_id not in skip_ids]

        # Sanitise collection name → profile name
        raw = self._collection.name or self._collection.slug or "Collection"
        profile_name = re.sub(r"[^\w\s\-]", "", raw).strip().replace(" ", "_")[:64] or "Collection"

        self._status_var.set(f"Creating profile '{profile_name}'…")
        try:
            profile_dir = _create_profile(
                self._game.name, profile_name, profile_specific_mods=True
            )
        except Exception as exc:
            self._status_var.set(f"Profile creation failed: {exc}")
            return

        self._log(f"Collection install: created profile '{profile_name}' at {profile_dir}")
        # Refresh the profile dropdown immediately so the new profile is visible
        self._refresh_profile_menu()
        self._status_var.set(f"Starting install of {len(mods)} mods into '{profile_name}'…")

        # Save the old profile dir so we can restore it after install
        old_profile = getattr(self._game, "_active_profile_dir", None)

        threading.Thread(
            target=self._run_install,
            args=(
                list(mods),
                self._download_link_path,
                profile_dir,
                old_profile,
                downloader,
                app,
                len(mods),
            ),
            daemon=True,
        ).start()

    def _run_install(self, mods, download_link_path, profile_dir, old_profile, downloader, app, total):
        """Background thread: download then install each mod in collection-defined order.

        Load order is driven by ``collection.json`` from the collection archive:
        - ``mods`` array defines install order (index 0 = lowest priority,
          last entry = highest priority).
        - ``plugins`` array defines the exact ``plugins.txt`` order.
        Both are written after all mods are installed.
        """
        self._game.set_active_profile_dir(profile_dir)
        modlist_path = profile_dir / "modlist.txt"
        plugins_path = profile_dir / "plugins.txt"
        staging_path = self._game.get_effective_mod_staging_path()
        installed = 0
        skipped = 0

        # ------------------------------------------------------------------
        # Step 1: Download and parse collection.json for authoritative order
        # ------------------------------------------------------------------
        collection_schema: dict = {}
        if download_link_path:
            # Reuse the schema already fetched when the detail panel opened,
            # if available — avoids downloading the archive twice.
            cached = getattr(self, "_collection_schema_cache", None)
            if cached:
                collection_schema = cached
                self._log("Collection install: reusing cached collection.json")
            else:
                try:
                    self.after(0, lambda: self._status_var.set("Downloading collection manifest…"))
                except Exception:
                    pass
                try:
                    collection_schema = self._api.get_collection_archive_json(download_link_path)
                    self._log(f"Collection install: parsed collection.json "
                              f"({len(collection_schema.get('mods', []))} mod entries, "
                              f"{len(collection_schema.get('plugins', []))} plugins)")
                except Exception as exc:
                    self._log(f"Collection install: could not download collection.json: {exc} — "
                              "continuing with GraphQL order")

        # Build a mapping from file_id → position in collection.json mods array
        # and file_id → pre-converted FOMOD auto-selections (if any)
        schema_mods: list[dict] = collection_schema.get("mods", [])
        schema_file_id_to_pos: dict[int, int] = {}
        schema_pos_to_name: dict[int, str] = {}  # collection.json logical name
        schema_file_id_to_logical: dict[int, str] = {}  # file_id → logicalFilename
        fomod_by_file_id: dict[int, dict] = {}   # file_id → saved_selections dict
        for pos, schema_mod in enumerate(schema_mods):
            src = schema_mod.get("source") or {}
            fid = src.get("fileId")
            if fid is not None:
                fid = int(fid)
                schema_file_id_to_pos[fid] = pos
                schema_pos_to_name[pos] = schema_mod.get("name") or ""
                logical = src.get("logicalFilename") or schema_mod.get("name") or ""
                schema_file_id_to_logical[fid] = logical
                choices = schema_mod.get("choices") or {}
                if choices.get("type") == "fomod":
                    fomod_by_file_id[fid] = _fomod_choices_from_collection(choices)

        # Sort the mods list by collection.json position when available;
        # mods without a position come last (preserving their original order).
        def _sort_key(m):
            return schema_file_id_to_pos.get(m.file_id, len(schema_mods))

        ordered_mods = sorted(mods, key=_sort_key)

        # ------------------------------------------------------------------
        # Step 2: Install each mod, tracking the folder names in order
        # ------------------------------------------------------------------
        # Pre-scan staging dir:
        #   already_installed_by_fid : file_id → folder name (from meta.ini fileid)
        #   staging_lower_map        : lower(folder_name) → actual folder name
        # Used together to skip mods already installed in a previous (partial) run.
        #
        # IMPORTANT: staging_path is the *shared* staging directory used by all
        # profiles for a game.  We must restrict the name-based staging_lower_map
        # to only the mods that are explicitly listed in *this* profile's
        # modlist.txt — otherwise mods installed for unrelated profiles will
        # produce false-positive "already installed" matches and be silently
        # skipped.  The file_id exact-match (already_installed_by_fid) is safe
        # to populate from all folders, because a file_id collision across
        # different mod pages is essentially impossible.
        already_installed_by_fid: dict[int, str] = {}  # file_id → staging folder name
        staging_lower_map: dict[str, str] = {}          # lower(name) → actual name

        # Build the set of mod folder names that are actually in this profile.
        _profile_mod_names: set[str] = set()
        if modlist_path.is_file():
            try:
                from Utils.modlist import read_modlist
                for entry in read_modlist(modlist_path):
                    _profile_mod_names.add(entry.name.lower())
            except Exception:
                pass

        import configparser as _cp
        if staging_path.exists():
            for mod_dir in staging_path.iterdir():
                if not mod_dir.is_dir():
                    continue
                # Name-based map: only include folders belonging to this profile.
                if mod_dir.name.lower() in _profile_mod_names:
                    staging_lower_map[mod_dir.name.lower()] = mod_dir.name
                meta_ini = mod_dir / "meta.ini"
                if not meta_ini.is_file():
                    continue
                try:
                    _parser = _cp.ConfigParser()
                    _parser.read(str(meta_ini), encoding="utf-8")
                    fid_str = _parser.get("General", "fileid", fallback="").strip()
                    if fid_str and fid_str != "0":
                        already_installed_by_fid[int(fid_str)] = mod_dir.name
                except Exception:
                    pass

        # Maps collection.json position (or fallback index) → installed folder name
        install_order: list[tuple[int, str]] = []  # (sort_key, folder_name)

        # ------------------------------------------------------------------
        # Classify: already-installed (skip) vs needs downloading
        # ------------------------------------------------------------------
        to_download: list = []  # CollectionMod objects that still need DL+install

        for mod in ordered_mods:
            if not mod.file_id:
                self._log(f"Collection install: skipping '{mod.mod_name}' — no file ID")
                skipped += 1
                continue

            # Check 1: fileid in meta.ini matches exactly
            existing_folder: str = ""
            if mod.file_id in already_installed_by_fid:
                existing_folder = already_installed_by_fid[mod.file_id]
            else:
                # Check 2: predicted folder name (logicalFilename / schema name / mod_name)
                logical = schema_file_id_to_logical.get(mod.file_id, "") or ""
                schema_name = schema_pos_to_name.get(schema_file_id_to_pos.get(mod.file_id, -1), "") or ""
                candidates: list[str] = []
                for raw in (logical, schema_name, mod.mod_name or ""):
                    if raw:
                        for s in _suggest_mod_names(raw):
                            if s and s not in candidates:
                                candidates.append(s)
                for candidate in candidates:
                    key = candidate.lower()
                    if key in staging_lower_map:
                        existing_folder = staging_lower_map[key]
                        break

            if existing_folder:
                self._log(f"Collection install: '{mod.mod_name}' already installed as '{existing_folder}' — skipping")
                install_order.append((_sort_key(mod), existing_folder))
                installed += 1
            else:
                to_download.append(mod)

        # ------------------------------------------------------------------
        # Step 2a: Download up to 3 mods in parallel
        # ------------------------------------------------------------------
        import concurrent.futures as _cf

        _DL_WORKERS = 3
        _dl_results: dict[int, object] = {}  # file_id → DownloadResult|None
        _dl_lock = threading.Lock()
        _dl_done = 0
        _dl_total = len(to_download)
        mod_panel = getattr(app, "_mod_panel", None)

        def _download_one(mod):
            nonlocal _dl_done

            # --- Create a stacked progress popup only for files >= 10 MB ---
            _POPUP_MIN_BYTES = 10 * 1024 * 1024
            dl_cancel = None
            if mod_panel is not None and getattr(mod, "size_bytes", 0) >= _POPUP_MIN_BYTES:
                _ce_holder: list = [None]
                _ce_ready = threading.Event()

                def _make_popup(mn=mod.mod_name):
                    try:
                        ce = mod_panel.get_download_cancel_event()
                        mod_panel.show_download_progress(mn, cancel=ce)
                        _ce_holder[0] = ce
                    except Exception:
                        pass
                    finally:
                        _ce_ready.set()

                try:
                    self.after(0, _make_popup)
                except Exception:
                    _ce_ready.set()
                _ce_ready.wait(timeout=5)
                dl_cancel = _ce_holder[0]

            def _progress_cb(cur: int, tot: int, _ce=dl_cancel):
                if mod_panel is None or _ce is None:
                    return
                try:
                    mod_panel.after(
                        0,
                        lambda c=cur, t=tot: mod_panel.update_download_progress(
                            c, t, cancel=_ce
                        ),
                    )
                except Exception:
                    pass

            # --- Download ---
            try:
                result = downloader.download_file(
                    game_domain=self._game_domain,
                    mod_id=mod.mod_id,
                    file_id=mod.file_id,
                    progress_cb=_progress_cb,
                    cancel=dl_cancel,
                    known_file_name=mod.file_name or "",
                    expected_size_bytes=getattr(mod, "size_bytes", 0) or 0,
                )
            except Exception as exc:
                self._log(f"Collection install: download failed for '{mod.mod_name}': {exc}")
                result = None

            # --- Hide popup ---
            if dl_cancel is not None and mod_panel is not None:
                try:
                    mod_panel.after(0, lambda ce=dl_cancel: mod_panel.hide_download_progress(cancel=ce))
                except Exception:
                    pass

            with _dl_lock:
                _dl_done += 1
                _dl_results[mod.file_id] = result
                done = _dl_done
            try:
                self.after(0, lambda d=done, t=_dl_total: self._status_var.set(
                    f"Downloading: {d}/{t} complete\u2026"
                ))
            except Exception:
                pass

        if to_download:
            try:
                self.after(0, lambda n=_dl_total, w=_DL_WORKERS: self._status_var.set(
                    f"Downloading {n} mod(s) — up to {w} at a time\u2026"
                ))
            except Exception:
                pass
            # Sort largest archives first so big downloads start immediately
            # and bandwidth is never idle waiting for a large mod at the end.
            _to_download_sorted = sorted(
                to_download,
                key=lambda m: getattr(m, "size_bytes", 0) or 0,
                reverse=True,
            )
            with _cf.ThreadPoolExecutor(max_workers=_DL_WORKERS) as _pool:
                list(_pool.map(_download_one, _to_download_sorted))

        # ------------------------------------------------------------------
        # Step 2b: Install downloaded archives in parallel worker threads.
        # ------------------------------------------------------------------
        # headless=True suppresses all GUI dialogs and per-mod modlist writes;
        # the collection manages modlist/plugins itself after all installs finish.
        # _INSTALL_WORKERS parallel extractions run at once — this keeps all CPU
        # cores and the NVMe busy without too much RAM pressure from py7zr.
        _INSTALL_WORKERS = 4

        # Count uses per physical archive path so we only delete it after the
        # last consumer finishes.  Two separate collection entries can reference
        # the same physical archive (same file_id, or different file_ids whose
        # cached-archive lookup resolved to the same file on disk).
        _archive_use_count: dict[str, int] = {}
        for _m in to_download:
            _r = _dl_results.get(_m.file_id) if _m.file_id else None
            if _r and _r.success and _r.file_path:
                _key = str(_r.file_path)
                _archive_use_count[_key] = _archive_use_count.get(_key, 0) + 1

        _install_lock = threading.Lock()
        _install_counters = {"installed": 0, "skipped": 0, "done": 0}
        _install_results: dict[int, str] = {}  # file_id → installed folder name

        def _install_one(mod):
            result = _dl_results.get(mod.file_id)
            if result is None or not result.success or not result.file_path:
                self._log(f"Collection install: download failed for '{mod.mod_name}'")
                with _install_lock:
                    _install_counters["skipped"] += 1
                    _install_counters["done"] += 1
                return

            archive_path = str(result.file_path)
            auto_fomod = fomod_by_file_id.get(mod.file_id)

            # Build prebuilt metadata so no extra API calls are needed.
            try:
                _pmeta = build_meta_from_download(
                    game_domain=self._game_domain,
                    mod_id=mod.mod_id,
                    file_id=mod.file_id,
                    archive_name=mod.file_name or "",
                )
                _pmeta.nexus_name = mod.mod_name or ""
                _pmeta.author = mod.mod_author or ""
                _pmeta.version = mod.version or ""
            except Exception:
                _pmeta = None

            folder_name = install_mod_from_archive(
                archive_path, self, self._log, self._game,
                fomod_auto_selections=auto_fomod,
                prebuilt_meta=_pmeta,
                profile_dir=profile_dir,
                headless=True,
            )

            with _install_lock:
                if folder_name:
                    _install_results[mod.file_id] = folder_name
                    _install_counters["installed"] += 1
                else:
                    _install_counters["skipped"] += 1
                _install_counters["done"] += 1
                done_so_far = _install_counters["done"]

                # Delete archive once all consumers of this path are done.
                if archive_path in _archive_use_count:
                    _archive_use_count[archive_path] -= 1
                    if _archive_use_count[archive_path] == 0:
                        try:
                            Path(archive_path).unlink(missing_ok=True)
                        except Exception as _del_exc:
                            self._log(
                                f"Collection install: could not remove archive "
                                f"'{archive_path}': {_del_exc}"
                            )

            # Update progress bar and mark row green on the main thread.
            try:
                self.after(0, lambda d=done_so_far, t=_dl_total: self._status_var.set(
                    f"Installing: {d}/{t} complete\u2026"
                ))
            except Exception:
                pass
            if mod.file_id and folder_name:
                try:
                    self.after(0, lambda fid=mod.file_id: self._mark_row_installed(fid))
                except Exception:
                    pass

        if to_download:
            try:
                self.after(0, lambda n=_dl_total, w=_INSTALL_WORKERS: self._status_var.set(
                    f"Installing {n} mod(s) — up to {w} at a time\u2026"
                ))
            except Exception:
                pass
            # Sort smallest archives first so workers stay busy and large mods
            # don't block the queue.  Any mod that needs a manual FOMOD dialog
            # will still serialize correctly via the _fomod_dialog_lock — running
            # small non-interactive mods first means the FOMOD prompts typically
            # appear after the bulk of parallel work is already done.
            _to_install = sorted(to_download, key=lambda m: getattr(m, "size_bytes", 0) or 0)
            with _cf.ThreadPoolExecutor(max_workers=_INSTALL_WORKERS) as _install_pool:
                list(_install_pool.map(_install_one, _to_install))

        installed += _install_counters["installed"]
        skipped  += _install_counters["skipped"]

        # Build install_order from parallel results.
        for mod in to_download:
            sort_key = _sort_key(mod)
            folder = (
                _install_results.get(mod.file_id)
                or schema_pos_to_name.get(sort_key)
                or mod.mod_name
            )
            if mod.file_id in _install_results:
                install_order.append((sort_key, folder))

        # ------------------------------------------------------------------
        # Step 3: Write modlist.txt in collection-defined order
        # (collection index 0 = lowest priority → last in modlist.txt;
        #  collection last entry = highest priority → first in modlist.txt)
        # ------------------------------------------------------------------
        install_order.sort(key=lambda x: x[0])  # sort by collection position
        # Highest priority first (reversed collection order)
        modlist_entries = [
            ModEntry(name=folder, enabled=True, locked=False)
            for _, folder in reversed(install_order)
        ]
        if modlist_entries:
            try:
                write_modlist(modlist_path, modlist_entries)
                self._log(f"Collection install: wrote modlist.txt with {len(modlist_entries)} entries")
            except Exception as exc:
                self._log(f"Collection install: failed to write modlist.txt: {exc}")

        # ------------------------------------------------------------------
        # Step 4: Write plugins.txt from collection.json if available
        # ------------------------------------------------------------------
        schema_plugins: list[dict] = collection_schema.get("plugins", [])
        if schema_plugins:
            try:
                lines = []
                for plugin in schema_plugins:
                    name = plugin.get("name", "")
                    enabled = plugin.get("enabled", True)
                    lines.append(("*" if enabled else "") + name)
                plugins_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
                self._log(f"Collection install: wrote plugins.txt with {len(lines)} plugins")
            except Exception as exc:
                self._log(f"Collection install: failed to write plugins.txt: {exc}")

        # Restore the original profile dir
        self._game.set_active_profile_dir(old_profile)

        try:
            self.after(0, lambda: self._on_install_done(installed, skipped, total, str(profile_dir.name)))
        except Exception:
            pass

    def _refresh_profile_menu(self):
        """Update the top-bar profile dropdown to include any newly created profiles."""
        try:
            topbar = getattr(self._app_root, "_topbar", None)
            if topbar is None:
                return
            profiles = _profiles_for_game(self._game.name)
            topbar._profile_menu.configure(values=profiles)
        except Exception:
            pass

    def _on_install_done(self, installed: int, skipped: int, total: int, profile_name: str):
        self._status_var.set(
            f"Done — {installed}/{total} mods installed into profile '{profile_name}'."
            + (f" ({skipped} skipped)" if skipped else "")
        )
        self._log(
            f"Collection install complete: {installed} installed, {skipped} skipped. "
            f"Switch to profile '{profile_name}' to use it."
        )
        self._refresh_profile_menu()
        self._update_reset_btn_visibility()

    # ------------------------------------------------------------------
    # Reset load order
    # ------------------------------------------------------------------
    def _get_profile_dir(self) -> "Path | None":
        """Return the profile directory for this collection, or None if it doesn't exist."""
        raw = self._collection.name or self._collection.slug or "Collection"
        profile_name = re.sub(r"[^\w\s\-]", "", raw).strip().replace(" ", "_")[:64] or "Collection"
        game = self._game
        try:
            profiles_root = game.get_profile_root()
        except AttributeError:
            from Utils.config_paths import get_profiles_dir
            profiles_root = get_profiles_dir() / (game.name if game else "")
        profile_dir = profiles_root / "profiles" / profile_name
        return profile_dir if profile_dir.is_dir() else None

    def _get_installed_mod_info(self) -> "tuple[set[str] | None, dict[int, str]]":
        """Return (lowercased installed mod folder names, file_id→folder_name) for the
        collection profile.  Returns (None, {}) if the profile doesn't exist.
        """
        profile_dir = self._get_profile_dir()
        if profile_dir is None:
            return None, {}

        modlist_path = profile_dir / "modlist.txt"
        try:
            entries = read_modlist(modlist_path)
            installed_names: set[str] = {e.name.lower() for e in entries if not e.is_separator}
        except Exception:
            installed_names = set()

        # Scan staging for file_id → folder_name (only folders present in modlist)
        file_id_to_folder: dict[int, str] = {}
        try:
            staging_path = self._game.get_effective_mod_staging_path()
            if staging_path.exists():
                import configparser as _cp
                for mod_dir in staging_path.iterdir():
                    if not mod_dir.is_dir():
                        continue
                    if mod_dir.name.lower() not in installed_names:
                        continue
                    meta_ini = mod_dir / "meta.ini"
                    if not meta_ini.is_file():
                        continue
                    try:
                        _parser = _cp.ConfigParser()
                        _parser.read(str(meta_ini), encoding="utf-8")
                        fid_str = _parser.get("General", "fileid", fallback="").strip()
                        if fid_str and fid_str != "0":
                            file_id_to_folder[int(fid_str)] = mod_dir.name
                    except Exception:
                        pass
        except Exception:
            pass

        return installed_names, file_id_to_folder

    def _update_reset_btn_visibility(self):
        """Show/hide the Reset Load Order button based on whether the profile exists."""
        if self._reset_btn is None:
            return
        try:
            if self._get_profile_dir() is not None:
                self._reset_btn.pack(side="left", padx=10, pady=6)
            else:
                self._reset_btn.pack_forget()
        except Exception:
            pass

    def _on_reset_load_order(self):
        """Show confirmation then launch background reset thread."""
        profile_dir = self._get_profile_dir()
        if profile_dir is None:
            self._status_var.set("Profile not found — install the collection first.")
            return
        if not self._download_link_path:
            self._status_var.set("Collection manifest URL not loaded yet — please wait.")
            return
        self._status_var.set("Resetting load order from collection manifest…")
        threading.Thread(
            target=self._run_reset_load_order,
            args=(profile_dir,),
            daemon=True,
        ).start()

    def _run_reset_load_order(self, profile_dir: Path):
        """Background: re-fetch collection.json and rewrite modlist.txt + plugins.txt."""
        import configparser
        try:
            self.after(0, lambda: self._status_var.set("Downloading collection manifest…"))
            cj = self._api.get_collection_archive_json(self._download_link_path)

            # Build file_id → collection position map
            fid_to_pos: dict = {}
            for pos, m in enumerate(cj.get("mods", [])):
                fid = (m.get("source") or {}).get("fileId")
                if fid is not None:
                    fid_to_pos[int(fid)] = pos

            # Staging dir for a profile-specific-mods profile is profile_dir/mods
            staging_path = profile_dir / "mods"
            if not staging_path.is_dir():
                self.after(0, lambda: self._status_var.set(
                    "No mods folder in profile — has the collection been installed?"
                ))
                return

            # Scan each mod folder for meta.ini → file_id → collection position
            ordered: list[tuple[int, str]] = []  # (position, folder_name)
            unordered: list[str] = []
            for folder in staging_path.iterdir():
                if not folder.is_dir():
                    continue
                meta = folder / "meta.ini"
                fid = None
                if meta.exists():
                    try:
                        cp = configparser.ConfigParser(strict=False)
                        cp.read(meta, encoding="utf-8")
                        raw_fid = (
                            cp.get("General", "fileid", fallback=None)
                            or cp.get("general", "fileid", fallback=None)
                        )
                        if raw_fid:
                            fid = int(raw_fid)
                    except Exception:
                        pass
                if fid is not None and fid in fid_to_pos:
                    ordered.append((fid_to_pos[fid], folder.name))
                else:
                    unordered.append(folder.name)

            ordered.sort(key=lambda x: x[0])
            # Highest priority first (reversed collection order → first in modlist.txt)
            modlist_entries = [
                ModEntry(name=name, enabled=True, locked=False)
                for _, name in reversed(ordered)
            ] + [
                ModEntry(name=name, enabled=True, locked=False)
                for name in unordered
            ]

            modlist_path = profile_dir / "modlist.txt"
            if modlist_entries:
                try:
                    write_modlist(modlist_path, modlist_entries)
                    self._log(f"Reset load order: wrote modlist.txt with {len(modlist_entries)} entries")
                except Exception as exc:
                    self._log(f"Reset load order: failed to write modlist.txt: {exc}")

            # Re-write plugins.txt from collection.json
            schema_plugins: list = cj.get("plugins", [])
            if schema_plugins:
                try:
                    lines = []
                    for plugin in schema_plugins:
                        name = plugin.get("name", "")
                        enabled = plugin.get("enabled", True)
                        lines.append(("*" if enabled else "") + name)
                    plugins_path = profile_dir / "plugins.txt"
                    plugins_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
                    self._log(f"Reset load order: wrote plugins.txt with {len(lines)} plugins")
                except Exception as exc:
                    self._log(f"Reset load order: failed to write plugins.txt: {exc}")

            msg = (
                f"Load order reset — {len(ordered)} mods ordered"
                + (f", {len(unordered)} unmatched (kept at bottom)." if unordered else ".")
            )
            self.after(0, lambda: self._status_var.set(msg))
            self.after(0, self._refresh_panels_after_reset)

        except Exception as exc:
            self._log(f"Reset load order failed: {exc}")
            self.after(0, lambda: self._status_var.set(f"Reset failed: {exc}"))

    def _refresh_panels_after_reset(self):
        """Reload the modlist and plugin panels so they reflect the newly written files."""
        app = self._app_root
        try:
            mod_panel = getattr(app, "_mod_panel", None)
            if mod_panel is not None:
                mod_panel.reload_after_install()
        except Exception as exc:
            self._log(f"Reset load order: could not refresh mod panel: {exc}")
        try:
            pp = getattr(app, "_plugin_panel", None)
            if pp is not None:
                pp_path = getattr(pp, "_plugins_path", None)
                pp_exts = getattr(pp, "_plugin_extensions", None)
                if pp_path and pp_exts:
                    pp.load_plugins(pp_path, pp_exts)
        except Exception as exc:
            self._log(f"Reset load order: could not refresh plugin panel: {exc}")


# ---------------------------------------------------------------------------
# CollectionsDialog
# ---------------------------------------------------------------------------

class CollectionsDialog(tk.Frame):
    """
    Collections browser panel — embeds inside the ModListPanel area.
    """

    def __init__(
        self,
        parent: tk.Widget,
        game_domain: str,
        api,
        game=None,
        log_fn: Optional[Callable] = None,
        app_root: Optional[tk.Widget] = None,
        on_close: Optional[Callable] = None,
    ):
        super().__init__(parent, bg=BG_DEEP)
        self._game_domain = game_domain
        self._api = api
        self._game = game
        self._app_root = app_root or parent.winfo_toplevel()
        self._log = log_fn or (lambda msg: None)
        self._on_close = on_close

        self._collections: list = []
        self._cards: list[CollectionCard] = []
        self._page: int = 0
        self._loading: bool = False
        self._search_active: bool = False
        self._img_cache: dict = {}
        self._img_loading: set = set()
        self._cols: int = _COLL_COLS

        self._build()
        self.after(50, self._load_page)

    # ------------------------------------------------------------------
    # Build UI
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # URL parsing helper
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_collection_url(url: str) -> tuple[str, str]:
        """
        Extract (slug, game_domain) from a Nexus Mods collection URL.

        Handles patterns like:
          https://www.nexusmods.com/skyrimspecialedition/collections/x2ezso
          https://www.nexusmods.com/games/skyrimspecialedition/collections/x2ezso
          https://next.nexusmods.com/skyrimspecialedition/collections/x2ezso
        Returns ('', '') if parsing fails.
        """
        m = re.search(
            r'nexusmods\.com/(?:games/)?([^/?#]+)/collections/([A-Za-z0-9_\-]+)',
            url,
        )
        if m:
            return m.group(2), m.group(1)
        return '', ''

    def _build(self):
        self.grid_rowconfigure(2, weight=1)  # canvas row
        self.grid_rowconfigure(0, weight=0)
        self.grid_rowconfigure(1, weight=0)
        self.grid_rowconfigure(3, weight=0)
        self.grid_columnconfigure(0, weight=1)

        # Toolbar
        toolbar = tk.Frame(self, bg=BG_HEADER, height=32)
        toolbar.grid(row=0, column=0, sticky="ew")
        toolbar.grid_propagate(False)

        # Close button — top-right, returns to modlist
        tk.Button(
            toolbar, text="✕ Close",
            bg="#6b3333", fg="#ffffff", activebackground="#8c4444",
            activeforeground="#ffffff",
            relief="flat", font=FONT_SMALL,
            bd=0, highlightthickness=0, cursor="hand2",
            command=self._do_close,
        ).pack(side="right", padx=(4, 8), pady=4)

        self._prev_btn = tk.Button(
            toolbar, text="← Prev",
            bg="#c07320", fg="#ffffff", activebackground="#d4832a",
            activeforeground="#ffffff",
            relief="flat", font=FONT_SMALL,
            bd=0, highlightthickness=0, cursor="hand2",
            command=self._go_prev_page,
            state="disabled",
        )
        self._prev_btn.pack(side="left", padx=(8, 4), pady=4)

        self._next_btn = tk.Button(
            toolbar, text="Next →",
            bg="#c07320", fg="#ffffff", activebackground="#d4832a",
            activeforeground="#ffffff",
            relief="flat", font=FONT_SMALL,
            bd=0, highlightthickness=0, cursor="hand2",
            command=self._go_next_page,
            state="disabled",
        )
        self._next_btn.pack(side="left", padx=4, pady=4)

        self._url_toggle_btn = tk.Button(
            toolbar, text="Open URL…",
            bg="#2d5a8e", fg="#ffffff", activebackground="#3d6faa",
            activeforeground="#ffffff",
            relief="flat", font=FONT_SMALL,
            bd=0, highlightthickness=0, cursor="hand2",
            command=self._toggle_url_bar,
        )
        self._url_toggle_btn.pack(side="left", padx=(4, 4), pady=4)

        self._status_label = tk.Label(
            toolbar, text="Loading collections…",
            font=FONT_SMALL, fg=TEXT_DIM, bg=BG_HEADER, anchor="w",
        )
        self._status_label.pack(side="left", padx=8, fill="x", expand=True)

        # URL bar (hidden by default — shown when "Open URL" button is pressed)
        self._url_bar = tk.Frame(self, bg=BG_HEADER, height=34)
        self._url_bar.grid(row=1, column=0, sticky="ew")
        self._url_bar.grid_propagate(False)
        self._url_bar.grid_remove()   # hidden until toggled

        tk.Label(
            self._url_bar, text="Collection URL:",
            font=FONT_SMALL, fg=TEXT_DIM, bg=BG_HEADER,
        ).pack(side="left", padx=(8, 4), pady=5)

        self._url_var = tk.StringVar()
        self._url_entry = tk.Entry(
            self._url_bar,
            textvariable=self._url_var,
            bg=BG_ROW, fg=TEXT_MAIN,
            insertbackground=TEXT_MAIN,
            relief="flat", font=FONT_SMALL,
            bd=2, highlightthickness=1,
            highlightbackground=BORDER, highlightcolor=ACCENT,
        )
        self._url_entry.pack(side="left", fill="x", expand=True, pady=5)
        self._url_entry.bind("<Return>", lambda _e: self._go_from_url())
        self._url_entry.bind(
            "<Control-a>",
            lambda _e: (self._url_entry.selection_range(0, "end"), "break")[-1],
        )
        self._url_entry.bind("<Escape>", lambda _e: self._toggle_url_bar())

        tk.Button(
            self._url_bar, text="Go",
            bg=ACCENT, fg="#ffffff", activebackground=ACCENT_HOV,
            activeforeground="#ffffff",
            relief="flat", font=FONT_SMALL,
            bd=0, highlightthickness=0, cursor="hand2",
            command=self._go_from_url,
        ).pack(side="left", padx=(4, 4), pady=5)

        tk.Button(
            self._url_bar, text="✕",
            bg="#b33a3a", fg="#ffffff", activebackground="#c94848",
            activeforeground="#ffffff",
            relief="flat", font=FONT_SMALL,
            bd=0, highlightthickness=0, cursor="hand2",
            command=self._toggle_url_bar,
        ).pack(side="left", padx=(0, 8), pady=5)

        # Scrollable card canvas
        canvas_frame = tk.Frame(self, bg=BG_DEEP, bd=0, highlightthickness=0)
        canvas_frame.grid(row=2, column=0, sticky="nsew")
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
        for w in (self._canvas, self._inner):
            w.bind("<Button-4>",   lambda e: self._scroll(-80))
            w.bind("<Button-5>",   lambda e: self._scroll(80))
            w.bind("<MouseWheel>", self._on_mousewheel)

        # Search bar
        search_bar = tk.Frame(self, bg=BG_HEADER, height=34)
        search_bar.grid(row=3, column=0, sticky="ew")
        search_bar.grid_propagate(False)

        tk.Label(
            search_bar, text="Search:",
            font=FONT_SMALL, fg=TEXT_DIM, bg=BG_HEADER,
        ).pack(side="left", padx=(8, 4), pady=5)

        self._search_var = tk.StringVar()
        self._search_entry = tk.Entry(
            search_bar,
            textvariable=self._search_var,
            bg=BG_ROW, fg=TEXT_MAIN,
            insertbackground=TEXT_MAIN,
            relief="flat", font=FONT_SMALL,
            bd=2, highlightthickness=1,
            highlightbackground=BORDER, highlightcolor=ACCENT,
        )
        self._search_entry.pack(side="left", fill="x", expand=True, pady=5)
        self._search_entry.bind("<Return>", lambda _e: self._do_search())
        self._search_entry.bind(
            "<Control-a>",
            lambda _e: (self._search_entry.selection_range(0, "end"), "break")[-1],
        )

        self._search_btn = tk.Button(
            search_bar, text="Search",
            bg=ACCENT, fg="#ffffff", activebackground=ACCENT_HOV,
            activeforeground="#ffffff",
            relief="flat", font=FONT_SMALL,
            bd=0, highlightthickness=0, cursor="hand2",
            command=self._do_search,
        )
        self._search_btn.pack(side="left", padx=(4, 4), pady=5)

        self._clear_btn = tk.Button(
            search_bar, text="✕",
            bg="#b33a3a", fg="#ffffff", activebackground="#c94848",
            activeforeground="#ffffff",
            relief="flat", font=FONT_SMALL,
            bd=0, highlightthickness=0, cursor="hand2",
            command=self._clear_search,
        )
        self._clear_btn.pack(side="left", padx=(0, 8), pady=5)

    def _do_close(self):
        """Close the collections panel and return to the modlist."""
        if self._on_close:
            self._on_close()
        else:
            self.place_forget()
            self.destroy()

    # ------------------------------------------------------------------
    # Open from URL
    # ------------------------------------------------------------------

    def _toggle_url_bar(self):
        """Show/hide the URL input bar."""
        if self._url_bar.winfo_ismapped():
            self._url_bar.grid_remove()
            self._url_toggle_btn.configure(bg="#2d5a8e", activebackground="#3d6faa")
        else:
            self._url_bar.grid()
            self._url_toggle_btn.configure(bg="#3d6faa", activebackground="#2d5a8e")
            self._url_entry.focus_set()

    def _go_from_url(self):
        """Parse the entered URL and open the matching collection detail."""
        url = self._url_var.get().strip()
        if not url:
            self._status_label.configure(text="Please enter a collection URL.")
            return

        slug, url_domain = self._parse_collection_url(url)
        if not slug:
            self._status_label.configure(
                text="Could not parse URL — expected …nexusmods.com/…/collections/<slug>"
            )
            return

        # Use the domain from the URL when it differs from the current game domain
        game_domain = url_domain or self._game_domain

        self._status_label.configure(text=f"Loading collection '{slug}'…")
        self._url_bar.grid_remove()
        self._url_toggle_btn.configure(bg="#2d5a8e", activebackground="#3d6faa")

        from Nexus.nexus_api import NexusCollection
        # The detail dialog fetches all data itself; we just need the slug.
        # Use the slug as a placeholder name — the dialog header will show it
        # until CollectionDetailDialog populates the real name from the API.
        col = NexusCollection(slug=slug, name=slug, game_domain=game_domain)
        self._open_detail(col)

    # ------------------------------------------------------------------
    # Canvas / scroll helpers
    # ------------------------------------------------------------------

    def _on_inner_configure(self, _event=None):
        self._canvas.configure(scrollregion=self._canvas.bbox("all"))

    def _on_canvas_configure(self, event):
        self._canvas.itemconfig(self._inner_id, width=event.width)
        if hasattr(self, '_regrid_after_id') and self._regrid_after_id:
            self.after_cancel(self._regrid_after_id)
        self._regrid_after_id = self.after(250, self._regrid_cards)

    def _scroll(self, units: int):
        self._canvas.yview_scroll(units, "units")

    def _on_mousewheel(self, event):
        direction = -1 if event.delta > 0 else 1
        self._scroll(direction * 10)

    def _bind_scroll(self, widget: tk.Widget):
        widget.bind("<Button-4>",   lambda e: self._scroll(-80), add="+")
        widget.bind("<Button-5>",   lambda e: self._scroll(80),  add="+")
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

    def _build_cards(self):
        self._clear_cards()
        for col in self._collections:
            card = CollectionCard(
                self._inner, col,
                on_view=lambda c=col: self._open_detail(c),
            )
            self._bind_scroll(card.card)
            self._cards.append(card)
        self._regrid_cards()
        self._load_images()

    def _open_detail(self, collection):
        self._close_detail()
        panel = CollectionDetailDialog(
            self, collection=collection,
            game_domain=self._game_domain, api=self._api,
            game=self._game, app_root=self._app_root, log_fn=self._log,
            on_close=self._close_detail,
        )
        panel.place(relx=0, rely=0, relwidth=1, relheight=1)
        self._detail_panel = panel

    def _close_detail(self):
        panel = getattr(self, "_detail_panel", None)
        if panel is not None:
            try:
                panel.destroy()
            except Exception:
                pass
            self._detail_panel = None

    def _regrid_cards(self):
        canvas_w = self._canvas.winfo_width() or (_COLL_COLS * (_COLL_W + CARD_PAD * 2))
        # Compute how many fixed-width cards fit across the available width
        cols = max(1, canvas_w // (_COLL_W + CARD_PAD * 2))
        self._cols = cols

        total_card_w = cols * _COLL_W + (cols - 1) * CARD_PAD
        x_pad = max(CARD_PAD, (canvas_w - total_card_w) // 2)

        for idx, c in enumerate(self._cards):
            col = idx % cols
            row = idx // cols
            c.card.grid(
                row=row, column=col,
                padx=(x_pad if col == 0 else CARD_PAD // 2,
                       x_pad if col == cols - 1 else CARD_PAD // 2),
                pady=CARD_PAD,
                sticky="n",
            )
        for c in range(cols):
            self._inner.grid_columnconfigure(c, weight=1)

    def _load_images(self):
        for card in self._cards:
            card.load_image_async(
                card._collection.tile_image_url or "",
                self._img_cache,
                self._img_loading,
                self,
            )

    # ------------------------------------------------------------------
    # Pagination
    # ------------------------------------------------------------------

    def _go_prev_page(self):
        if self._page > 0 and not self._loading and not self._search_active:
            self._page -= 1
            self._load_page()

    def _go_next_page(self):
        if not self._loading and not self._search_active:
            if len(self._collections) >= PAGE_SIZE:
                self._page += 1
                self._load_page()

    def _load_page(self):
        if self._api is None:
            self._status_label.configure(text="No API key — set it via the Nexus button.")
            return
        if self._loading:
            return
        self._loading = True
        self._prev_btn.configure(state="disabled")
        self._next_btn.configure(state="disabled")
        page = self._page
        self._status_label.configure(text=f"Loading page {page + 1}…")

        def _worker():
            try:
                cols = self._api.get_collections(
                    self._game_domain, count=PAGE_SIZE, offset=page * PAGE_SIZE
                )
                self.after(0, lambda: self._on_loaded(cols, page, search=False))
            except Exception as exc:
                self.after(0, lambda: self._on_error(exc))

        threading.Thread(target=_worker, daemon=True).start()

    def _on_loaded(self, cols: list, page: int, search: bool):
        self._collections = cols
        self._loading = False
        self._prev_btn.configure(state="normal" if page > 0 else "disabled")
        self._next_btn.configure(
            state="normal" if (not search and len(cols) >= PAGE_SIZE) else "disabled"
        )
        self._build_cards()
        self._canvas.yview_moveto(0)
        label = f"Page {page + 1} — {len(cols)} collection(s)"
        if search:
            label = f"{len(cols)} result(s) for '{self._search_var.get().strip()}'"
        self._status_label.configure(text=label)
        self._log(f"Collections: {label}.")

    def _on_error(self, exc: Exception):
        self._loading = False
        self._prev_btn.configure(state="normal" if self._page > 0 else "disabled")
        self._next_btn.configure(state="normal")
        self._status_label.configure(text=f"Error: {exc}")
        self._log(f"Collections: Error — {exc}")

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    def _do_search(self):
        query_text = self._search_var.get().strip()
        if not query_text:
            return
        if self._api is None:
            self._status_label.configure(text="No API key — set it via the Nexus button.")
            return
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
                cols = self._api.search_collections(
                    self._game_domain, query_text, count=PAGE_SIZE, offset=0
                )
                self.after(0, lambda: self._on_search_done(cols, query_text))
            except Exception as exc:
                self.after(0, lambda: self._on_search_error(exc))

        threading.Thread(target=_worker, daemon=True).start()

    def _on_search_done(self, cols: list, query_text: str):
        self._collections = cols
        self._loading = False
        self._search_btn.configure(state="normal")
        self._prev_btn.configure(state="disabled")
        self._next_btn.configure(state="disabled")
        self._build_cards()
        self._canvas.yview_moveto(0)
        label = f"{len(cols)} result(s) for '{query_text}'"
        self._status_label.configure(text=label)
        self._log(f"Collections: {label}.")

    def _on_search_error(self, exc: Exception):
        self._loading = False
        self._search_btn.configure(state="normal")
        self._search_active = False
        self._status_label.configure(text=f"Search error: {exc}")
        self._log(f"Collections: Search failed — {exc}")

    def _clear_search(self):
        self._search_var.set("")
        self._search_active = False
        self._search_btn.configure(state="normal")
        self._collections = []
        self._clear_cards()
        self._page = 0
        self._prev_btn.configure(state="disabled")
        self._next_btn.configure(state="disabled")
        self._status_label.configure(text="Loading collections…")
        self._load_page()
