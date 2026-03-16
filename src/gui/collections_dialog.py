"""
collections_dialog.py
Browse Nexus Mods Collections for the currently selected game via GraphQL.

Opens as a standalone Toplevel window.  Displays 20 collections per page,
sorted by most downloaded by default.  Includes a search bar to filter
by name, and Prev / Next page navigation.
"""

from __future__ import annotations

import os
import re
import threading
import tkinter as tk
import tkinter.ttk as ttk
import webbrowser
from pathlib import Path
from typing import Callable, Optional

import customtkinter as ctk
from PIL import Image

from gui.ctk_components import CTkAlert
from gui.dialogs import CollectionInstallModeDialog
from gui.game_helpers import (
    _create_profile,
    _profiles_for_game,
    get_collection_url_from_profile,
    save_collection_url_to_profile,
)
from gui.install_mod import install_mod_from_archive
from gui.mod_card import CARD_PAD, make_placeholder_image
from Utils.ui_config import get_ui_scale
from gui.mod_name_utils import _suggest_mod_names
from Utils.modlist import write_modlist, read_modlist, ModEntry
from Utils.filemap import rebuild_mod_index
from Utils.config_paths import get_download_cache_dir
from Nexus.nexus_download import delete_archive_and_sidecar
from Nexus.nexus_meta import build_meta_from_download
from Utils.xdg import open_url

# Collections-specific card dimensions (5-column grid)
_COLL_COLS  = 5
_COLL_W     = 220  # 200 was too narrow at 1.25x–1.5x scale; extra width avoids clipping
_COLL_IMG_W = 210
_COLL_IMG_H = 240
from gui.theme import (
    BG_DEEP,
    BG_PANEL,
    BG_HEADER,
    BG_ROW,
    BG_SEP,
    BG_HOVER,
    ACCENT,
    ACCENT_HOV,
    TEXT_MAIN,
    TEXT_DIM,
    BORDER,
    FONT_HEADER,
    FONT_NORMAL,
    FONT_BOLD,
    FONT_SMALL,
    font_sized,
    font_sized_px,
    scaled,
)

PAGE_SIZE    = 20


def _topo_sort_collection(schema_mods: list[dict], mod_rules: list[dict]) -> dict[int, int]:
    """Return file_id → priority-position dict respecting modRules before/after constraints.

    Position 0 = highest priority (wins conflicts), higher number = lower priority.
    Falls back to the mods-array order for any mod not constrained by rules.
    Cycles are broken by ignoring the offending edge (Kahn's algorithm skips them naturally).
    """
    # Build logical_name → file_id map from the mods array
    logical_to_fid: dict[str, int] = {}
    fid_order: list[int] = []  # original mods-array order, used as topo fallback
    for m in schema_mods:
        src = m.get("source") or {}
        fid = src.get("fileId")
        if fid is None:
            continue
        fid = int(fid)
        logical = (src.get("logicalFilename") or m.get("name") or "").strip()
        if logical:
            logical_to_fid[logical] = fid
        if fid not in fid_order:
            fid_order.append(fid)

    all_fids: set[int] = set(fid_order)

    # edges: higher_priority_fid → {lower_priority_fids}
    # "source after reference"  → reference has higher priority than source
    # "source before reference" → source has higher priority than reference
    higher_than: dict[int, set[int]] = {f: set() for f in all_fids}  # fid → fids it beats
    in_degree: dict[int, int] = {f: 0 for f in all_fids}

    def _resolve(name: str) -> int | None:
        return logical_to_fid.get(name)

    for rule in mod_rules:
        rtype = rule.get("type")
        if rtype not in ("before", "after"):
            continue
        ref_name = (rule.get("reference") or {}).get("logicalFileName", "")
        src_name = (rule.get("source") or {}).get("logicalFileName", "")
        ref_fid = _resolve(ref_name)
        src_fid = _resolve(src_name)
        if ref_fid is None or src_fid is None or ref_fid == src_fid:
            continue

        if rtype == "after":
            # source loads after reference → source wins (loads on top of reference)
            winner, loser = src_fid, ref_fid
        else:  # "before"
            # source loads before reference → reference wins
            winner, loser = ref_fid, src_fid

        if loser not in higher_than[winner]:
            higher_than[winner].add(loser)
            in_degree[loser] += 1

    # Kahn's topological sort — highest priority first
    from collections import deque
    queue = deque(f for f in fid_order if in_degree[f] == 0)
    sorted_fids: list[int] = []
    remaining = set(fid_order)

    while queue:
        fid = queue.popleft()
        if fid not in remaining:
            continue
        remaining.discard(fid)
        sorted_fids.append(fid)
        # Process dependents in original-array order for determinism
        for dep in sorted(higher_than[fid], key=lambda f: fid_order.index(f) if f in fid_order else 999999):
            in_degree[dep] -= 1
            if in_degree[dep] == 0:
                queue.append(dep)

    # Append any fids not reached (cycle members) in original order
    for fid in fid_order:
        if fid in remaining:
            sorted_fids.append(fid)

    # sorted_fids[0] = highest priority → position 0
    return {fid: pos for pos, fid in enumerate(sorted_fids)}


def _fmt_size(n_bytes: int) -> str:
    """Human-readable file size."""
    if n_bytes <= 0:
        return "—"
    for unit, threshold in (("GB", 1 << 30), ("MB", 1 << 20), ("KB", 1 << 10)):
        if n_bytes >= threshold:
            return f"{n_bytes / threshold:.1f} {unit}"
    return f"{n_bytes} B"


def _get_dir_size(path: Path) -> int:
    """Return the total byte size of a directory (recursive). Returns 0 for missing/non-dir."""
    if not path.is_dir():
        return 0
    total = 0
    try:
        for p in path.rglob("*"):
            if p.is_file():
                total += p.stat().st_size
    except OSError:
        pass
    return total


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
        self._coll_w = scaled(_COLL_W)
        self._coll_img_w = scaled(_COLL_IMG_W)
        self._coll_img_h = scaled(_COLL_IMG_H)
        # Text area: name + stats + author only (summary moved to hover tooltip).
        s = get_ui_scale()
        text_h = max(50, int(90 * s))
        # Include image pady so card height matches actual layout (image + pady + btn + text)
        _img_pady = scaled(6) + scaled(3)
        _btn_row_h = scaled(60)  # taller at high scale so View button is fully visible
        self._coll_h = self._coll_img_h + _img_pady + _btn_row_h + text_h

        # Outer card frame — fixed size, content clips if too long.
        self.card = tk.Frame(
            parent,
            width=self._coll_w, height=self._coll_h,
            bg=BG_PANEL,
            highlightbackground=BORDER,
            highlightthickness=1,
        )
        self.card.pack_propagate(False)
        self.card.grid_propagate(False)

        self._build(on_view)

    def _build(self, on_view: Callable):
        col = self._collection

        # Tile image placeholder — use unscaled dims for CTkImage/CTkLabel since
        # CTk applies set_widget_scaling internally (scaled() would double-scale).
        placeholder = make_placeholder_image(_COLL_IMG_W, _COLL_IMG_H)
        ph_ctk = ctk.CTkImage(light_image=placeholder, dark_image=placeholder,
                               size=(_COLL_IMG_W, _COLL_IMG_H))
        self._img_label = ctk.CTkLabel(
            self.card, image=ph_ctk, text="",
            width=_COLL_IMG_W, height=_COLL_IMG_H,
        )

        _btn_row_h = scaled(60)
        text_h = self._coll_h - self._coll_img_h - scaled(6) - scaled(3) - _btn_row_h
        text_frame = tk.Frame(self.card, bg=BG_PANEL, height=text_h)
        text_frame.pack_propagate(False)

        btn_frame = tk.Frame(self.card, bg=BG_PANEL, height=_btn_row_h)
        btn_frame.pack_propagate(False)
        # CTk scales widget width/height via set_widget_scaling(); use unscaled design
        # values so CTk scales once to fit the card (avoid double-scaling overflow)
        _btn_w = _COLL_W - 20
        _btn_h = 28
        ctk.CTkButton(
            btn_frame, text="View",
            width=_btn_w, height=_btn_h,
            fg_color=ACCENT, hover_color=ACCENT_HOV,
            text_color="#ffffff", font=FONT_SMALL,
            command=on_view,
        ).place(relx=0.5, rely=0.5, anchor="center")

        # Use grid: row0=image (fixed), row2=btn (fixed), row1=text (flexible remainder).
        # No minsize on row1 — it gets whatever is left so btn row never overflows the card.
        self.card.grid_rowconfigure(0, minsize=self._coll_img_h + scaled(6) + scaled(3), weight=0)
        self.card.grid_rowconfigure(1, weight=1)
        self.card.grid_rowconfigure(2, minsize=_btn_row_h, weight=0)
        pad = scaled(5)
        self._img_label.grid(row=0, column=0, padx=pad, pady=(scaled(6), scaled(3)), sticky="n")
        text_frame.grid(row=1, column=0, sticky="nsew")
        btn_frame.grid(row=2, column=0, sticky="ew")
        self.card.grid_columnconfigure(0, weight=1)

        # Use tk.Label (not CTkLabel) so wraplength is in pixels with no CTk scaling
        _wrap = self._coll_w - scaled(16)
        # Name
        name_text = col.name or f"Collection {col.id}"
        tk.Label(
            text_frame, text=name_text,
            bg=BG_PANEL, fg=TEXT_MAIN,
            font=FONT_BOLD,
            wraplength=_wrap, justify="left", anchor="w",
        ).pack(padx=scaled(8), fill="x")

        # Stats: downloads, endorsements, mod count
        stats = f"↓{col.total_downloads:,}  ♥{col.endorsements:,}  {col.mod_count} mods"
        tk.Label(
            text_frame, text=stats,
            bg=BG_PANEL, fg=TEXT_DIM,
            font=FONT_SMALL,
            anchor="w", wraplength=_wrap,
        ).pack(padx=scaled(8), fill="x")

        # Author
        if col.user_name:
            tk.Label(
                text_frame, text=f"by {col.user_name}",
                bg=BG_PANEL, fg=TEXT_DIM,
                font=FONT_SMALL,
                anchor="w", wraplength=_wrap,
            ).pack(padx=scaled(8), fill="x")

        # Summary shown as a hover tooltip on the card instead of inline text.
        summary = (col.summary or "").strip()
        if summary:
            self._attach_tooltip(self.card, summary)

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
                # Scale to cover the slot (zoom), then center-crop.
                # Use unscaled design dims — CTk applies set_widget_scaling internally.
                src_w, src_h = raw.size
                iw, ih = _COLL_IMG_W, _COLL_IMG_H
                scale = max(iw / src_w, ih / src_h)
                new_w = int(src_w * scale)
                new_h = int(src_h * scale)
                raw = raw.resize((new_w, new_h), Image.LANCZOS)
                x_off = (new_w - iw) // 2
                y_off = (new_h - ih) // 2
                bg = raw.crop((x_off, y_off, x_off + iw, y_off + ih))
                photo = ctk.CTkImage(light_image=bg, dark_image=bg,
                                     size=(iw, ih))
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

    # ------------------------------------------------------------------
    # Hover tooltip for collection summary
    # ------------------------------------------------------------------

    def _attach_tooltip(self, widget: tk.Widget, text: str) -> None:
        """Attach a hover tooltip showing *text* to *widget* and all its children."""
        self._tooltip_win: tk.Toplevel | None = None
        self._tooltip_text = text

        def _enter(event):
            if self._tooltip_win is not None:
                return
            tw = tk.Toplevel(widget)
            tw.overrideredirect(True)
            tw.attributes("-alpha", 0.95)
            tw.configure(bg=BG_DEEP)
            wrap = min(scaled(340), scaled(int(self._coll_w * 1.4)))
            tk.Label(
                tw, text=self._tooltip_text,
                bg=BG_DEEP, fg=TEXT_MAIN,
                font=FONT_SMALL,
                wraplength=wrap, justify="left",
                padx=scaled(8), pady=scaled(6),
            ).pack()
            # Position near cursor, keep on-screen
            x = event.x_root + scaled(12)
            y = event.y_root + scaled(12)
            tw.update_idletasks()
            sw = tw.winfo_screenwidth()
            sh = tw.winfo_screenheight()
            if x + tw.winfo_reqwidth() > sw:
                x = event.x_root - tw.winfo_reqwidth() - scaled(4)
            if y + tw.winfo_reqheight() > sh:
                y = event.y_root - tw.winfo_reqheight() - scaled(4)
            tw.geometry(f"+{x}+{y}")
            self._tooltip_win = tw

        def _leave(event):
            if self._tooltip_win:
                self._tooltip_win.destroy()
                self._tooltip_win = None

        def _bind_recursive(w: tk.Widget) -> None:
            w.bind("<Enter>", _enter, add="+")
            w.bind("<Leave>", _leave, add="+")
            for child in w.winfo_children():
                _bind_recursive(child)

        _bind_recursive(widget)


# ---------------------------------------------------------------------------
# OptionalModsPanel — inline overlay for plugin panel
# ---------------------------------------------------------------------------

class OptionalModsPanel(ctk.CTkFrame):
    """
    Inline panel that overlays the plugin panel. Lists optional mods with checkboxes
    (all checked by default). Show before installing a collection so the user can
    deselect mods they do not want.

    result: None = cancelled; set of file_ids = optional mods to **skip**.
    on_done(panel) is called when user clicks Install or Cancel.
    """

    def __init__(self, parent, optional_mods: list, on_done=None):
        super().__init__(parent, fg_color=BG_DEEP, corner_radius=0)
        self.result = None
        self._optional_mods = optional_mods
        self._vars: dict[int, tk.BooleanVar] = {}
        self._on_done = on_done or (lambda p: None)

        # Title bar
        title_bar = ctk.CTkFrame(self, fg_color=BG_PANEL, corner_radius=0, height=36)
        title_bar.pack(fill="x")
        title_bar.pack_propagate(False)
        ctk.CTkLabel(
            title_bar, text="Optional Mods",
            font=FONT_BOLD, text_color=TEXT_MAIN, anchor="w",
        ).pack(side="left", padx=12)
        ctk.CTkFrame(self, fg_color=BORDER, height=1, corner_radius=0).pack(fill="x")

        # Subtitle
        ctk.CTkLabel(
            self,
            text=(f"{len(optional_mods)} optional mod(s) found. "
                  "Uncheck any you do not want installed:"),
            font=FONT_SMALL, text_color=TEXT_DIM, anchor="w",
        ).pack(anchor="w", padx=16, pady=(12, 6))

        scroll = ctk.CTkScrollableFrame(self, fg_color=BG_PANEL, corner_radius=6)
        scroll.pack(fill="both", expand=True, padx=12, pady=(0, 4))
        scroll.grid_columnconfigure(0, weight=1)

        for mod in optional_mods:
            var = tk.BooleanVar(value=True)
            self._vars[mod.file_id] = var
            name_text = mod.mod_name or mod.file_name or "(Unknown)"
            author_text = f" by {mod.mod_author}" if mod.mod_author else ""
            row = ctk.CTkFrame(scroll, fg_color="transparent")
            row.grid_columnconfigure(0, weight=1)
            row.grid(sticky="ew")
            ctk.CTkCheckBox(
                row,
                text=f"{name_text}{author_text}",
                variable=var,
                font=FONT_NORMAL,
                text_color=TEXT_MAIN,
                fg_color=ACCENT,
                hover_color=ACCENT_HOV,
                checkmark_color="white",
                border_color=BORDER,
            ).grid(row=0, column=0, sticky="w", padx=8, pady=3)

        helper = ctk.CTkFrame(self, fg_color="transparent")
        helper.pack(fill="x", padx=12, pady=(0, 4))
        ctk.CTkButton(
            helper, text="Select All", width=90, height=24, font=FONT_SMALL,
            fg_color=BG_HEADER, hover_color=BG_PANEL, text_color=TEXT_MAIN,
            command=self._select_all,
        ).pack(side="left", padx=(0, 6))
        ctk.CTkButton(
            helper, text="Deselect All", width=90, height=24, font=FONT_SMALL,
            fg_color=BG_HEADER, hover_color=BG_PANEL, text_color=TEXT_MAIN,
            command=self._deselect_all,
        ).pack(side="left")

        bar = ctk.CTkFrame(self, fg_color=BG_PANEL, corner_radius=0, height=52)
        bar.pack(fill="x")
        bar.pack_propagate(False)
        ctk.CTkFrame(bar, fg_color=BORDER, height=1, corner_radius=0).pack(side="top", fill="x")
        ctk.CTkButton(
            bar, text="Cancel", width=80, height=28, font=FONT_NORMAL,
            fg_color=BG_HEADER, hover_color=BG_HOVER, text_color=TEXT_MAIN,
            command=self._on_cancel,
        ).pack(side="right", padx=(4, 12), pady=12)
        ctk.CTkButton(
            bar, text="Install", width=80, height=28, font=FONT_BOLD,
            fg_color="#2d7a2d", hover_color="#3a9e3a", text_color="white",
            command=self._on_ok,
        ).pack(side="right", padx=4, pady=12)

    def _on_ok(self):
        self.result = {fid for fid, var in self._vars.items() if not var.get()}
        self._on_done(self)

    def _on_cancel(self):
        self.result = None
        self._on_done(self)

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
    _TV_WIDTHS_BASE = (50, 250, 120, 200, 80, 40)

    def __init__(self, parent, collection, game_domain: str, api, game=None, app_root=None, log_fn=None, on_close=None, profile_dir=None):
        super().__init__(parent, bg=BG_DEEP)
        self._collection = collection
        self._game_domain = game_domain
        self._api = api
        self._game = game
        self._app_root = app_root
        self._log = log_fn or (lambda *a: None)
        self._on_close = on_close or self.destroy
        self._profile_dir_override = profile_dir  # when set, use instead of deriving from collection name

        self._name_var = tk.StringVar(value=collection.name or collection.slug or "Collection")
        self._size_var = tk.StringVar(value="Loading\u2026")
        self._status_var = tk.StringVar(value="Fetching mod list\u2026")
        self._loaded_mods: list = []
        self._download_link_path: str = ""
        self._schema_order: dict = {}

        self._reset_btn = None  # created in _build_ui; shown only when profile exists
        self._file_id_to_tree_iid: dict[int, str] = {}  # populated by _populate; used to green rows live

        self._build_ui()
        self._fetch()
        self.after(100, lambda: (self._update_reset_btn_visibility(), self._update_open_missing_btn_visibility()))

    # ------------------------------------------------------------------
    def _build_ui(self):
        col = self._collection

        # --- Header bar ---
        hdr = tk.Frame(self, bg=BG_HEADER, pady=8, bd=0, highlightthickness=0)
        hdr.pack(fill="x", side="top")

        tk.Label(
            hdr, textvariable=self._name_var,
            bg=BG_HEADER, fg=TEXT_MAIN,
            font=font_sized_px("Segoe UI", 13, "bold"),
            anchor="w",
        ).pack(side="left", padx=14)

        tk.Label(
            hdr, textvariable=self._size_var,
            bg=BG_HEADER, fg=TEXT_DIM,
            font=font_sized_px("Segoe UI", 10),
            anchor="e",
        ).pack(side="right", padx=14)

        # --- Status bar ---
        self._status_lbl = tk.Label(
            self, textvariable=self._status_var,
            bg=BG_DEEP, fg=TEXT_DIM,
            font=font_sized_px("Segoe UI", 9),
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
        import gui.theme as _theme
        style.configure(
            "CollDetail.Treeview",
            background=BG_PANEL, foreground=TEXT_MAIN,
            fieldbackground=BG_PANEL, rowheight=scaled(24),
            font=("Segoe UI", _theme.FS9),
            borderwidth=0, relief="flat",
        )
        style.configure(
            "CollDetail.Treeview.Heading",
            background=BG_HEADER, foreground=TEXT_MAIN,
            font=("Segoe UI", _theme.FS9, "bold"),
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
        tv_widths = tuple(scaled(w) for w in self._TV_WIDTHS_BASE)
        for col_id, width in zip(self._TV_COLS, tv_widths):
            anchor = "center" if col_id == "Order" else "w"
            self._tree.heading(col_id, text=col_id, anchor=anchor)
            stretch = col_id in ("Mod Name", "File")
            self._tree.column(col_id, width=width, minwidth=scaled(30), anchor=anchor, stretch=stretch)

        self._tree.tag_configure("odd", background=BG_ROW)
        self._tree.tag_configure("even", background=BG_PANEL)
        self._tree.tag_configure("unordered", foreground="#888888")
        self._tree.tag_configure("installed", background="#1e4d1e")

        # --- Priority note ---
        note = tk.Label(
            self, text="Order = author's install order  (↓ installed last = highest priority)",
            bg=BG_DEEP, fg=TEXT_DIM, font=font_sized_px("Segoe UI", 8), anchor="w",
        )
        note.pack(fill="x", side="top", padx=10, pady=(0, 2))

        # --- Footer ---
        ftr = tk.Frame(self, bg=BG_HEADER, pady=8, bd=0, highlightthickness=0)
        ftr.pack(fill="x", side="bottom")

        ctk.CTkButton(
            ftr, text="Close",
            height=scaled(30), fg_color="#3c3c3c", hover_color="#505050",
            text_color=TEXT_MAIN, font=font_sized("Segoe UI", 10),
            border_width=0,
            command=self._on_close,
        ).pack(side="right", padx=10, pady=6)

        ctk.CTkButton(
            ftr, text="Install Collection",
            height=scaled(30), fg_color="#2d7a2d", hover_color="#3a9e3a",
            text_color="#ffffff", font=font_sized("Segoe UI", 10, "bold"),
            border_width=0,
            command=self._on_install_collection,
        ).pack(side="right", padx=(10, 0), pady=6)

        self._open_missing_btn = ctk.CTkButton(
            ftr, text="Open Missing on Nexus",
            height=scaled(30), fg_color="#5a3a00", hover_color="#7a5200",
            text_color="#ffffff", font=font_sized("Segoe UI", 10),
            border_width=0,
            command=self._on_open_missing_on_nexus,
        )
        # Shown only when collection is installed and has missing mods; see _update_open_missing_btn_visibility()

        self._reset_btn = ctk.CTkButton(
            ftr, text="Reset Load Order",
            height=scaled(30), fg_color="#5a3a00", hover_color="#7a5200",
            text_color="#ffffff", font=font_sized("Segoe UI", 10),
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

            # Update collection name from API (fixes slug-only placeholder when opened via URL/nxm)
            if name:
                self._collection.name = name
            try:
                self.after(0, lambda: self._populate(
                    name or self._collection.slug or "Collection",
                    total_size, mod_count, mods, dl_path, schema_order))
            except Exception:
                pass
        except Exception as exc:
            self._log(f"CollectionDetail error: {exc}")
            try:
                self.after(0, lambda: self._status_var.set(f"Error: {exc}"))
            except Exception:
                pass

    def _populate(self, collection_name: str, total_size: int, mod_count: int, mods, dl_path: str = "", schema_order=None):
        try:
            if not self.winfo_exists():
                return
        except Exception:
            return
        schema_order = schema_order or {}
        self._name_var.set(collection_name)
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

        self._update_open_missing_btn_visibility()

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

        # --- Optional mods selection (overlay on plugin panel) ---
        optional_mods = [m for m in mods if m.optional]
        if optional_mods:
            show_fn = getattr(app, "show_optional_mods_panel", None)
            if show_fn:
                def _on_optional_done(panel):
                    if panel.result is None:
                        return
                    mods_to_use = list(mods)
                    if panel.result:
                        mods_to_use = [
                            m for m in mods_to_use
                            if not m.optional or m.file_id not in panel.result
                        ]
                    self._continue_install_collection(app, mods_to_use, downloader)
                show_fn(optional_mods, _on_optional_done)
                return
            # Fallback: no app overlay support — skip optional selection and install all
            mods = [m for m in mods if not m.optional]

        self._continue_install_collection(app, list(mods), downloader)

    def _continue_install_collection(self, app, mods, downloader):
        """Proceed with collection install after optional mods have been resolved."""
        if not self._game:
            return

        existing_profiles = _profiles_for_game(self._game.name)
        plugin_panel = getattr(app, "_plugin_panel", None)
        overlay_parent = plugin_panel if plugin_panel is not None else self

        def _on_mode_chosen(result):
            try:
                overlay.destroy()
            except Exception:
                pass
            if result is None:
                self._status_var.set("Install cancelled.")
                return
            self._finish_install_collection(app, mods, downloader, result)

        overlay = CollectionInstallModeDialog(overlay_parent, existing_profiles, _on_mode_chosen)
        overlay.place(relx=0, rely=0, relwidth=1, relheight=1)
        overlay.lift()

    def _finish_install_collection(self, app, mods, downloader, mode_result):
        """Called after the install-mode overlay is dismissed."""
        if not self._game:
            return

        mode, append_profile_name, overwrite_existing = mode_result

        if mode == "new":
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
            # Store collection URL in profile_settings.json for "Open Current" button
            game_domain = getattr(self._game, "nexus_game_domain", None) or self._game_domain
            collection_url = f"https://www.nexusmods.com/{game_domain}/collections/{self._collection.slug}"
            save_collection_url_to_profile(profile_dir, collection_url)
            # Refresh the profile dropdown immediately so the new profile is visible
            self._refresh_profile_menu()
        else:
            # Append into an existing profile
            profile_name = append_profile_name
            profile_root = self._game.get_profile_root()
            profile_dir = profile_root / "profiles" / profile_name
            if not profile_dir.is_dir():
                self._status_var.set(f"Profile '{profile_name}' not found.")
                return
            self._log(f"Collection install: appending into existing profile '{profile_name}' at {profile_dir}")

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
                None if mode == "new" else overwrite_existing,
            ),
            daemon=True,
        ).start()

    def _run_install(self, mods, download_link_path, profile_dir, old_profile, downloader, app, total, overwrite_existing: "bool | None" = None):
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

        # Build a mapping from file_id → priority position (0 = highest priority)
        # respecting modRules before/after constraints via topological sort.
        schema_mods: list[dict] = collection_schema.get("mods", [])
        mod_rules: list[dict] = collection_schema.get("modRules", [])
        schema_file_id_to_pos: dict[int, int] = _topo_sort_collection(schema_mods, mod_rules)
        schema_pos_to_name: dict[int, str] = {}  # collection.json logical name
        schema_file_id_to_logical: dict[int, str] = {}  # file_id → logicalFilename
        fomod_by_file_id: dict[int, dict] = {}   # file_id → saved_selections dict
        for pos, schema_mod in enumerate(schema_mods):
            src = schema_mod.get("source") or {}
            fid = src.get("fileId")
            if fid is not None:
                fid = int(fid)
                topo_pos = schema_file_id_to_pos.get(fid, pos)
                schema_pos_to_name[topo_pos] = schema_mod.get("name") or ""
                logical = src.get("logicalFilename") or schema_mod.get("name") or ""
                schema_file_id_to_logical[fid] = logical
                choices = schema_mod.get("choices") or {}
                if choices.get("type") == "fomod":
                    fomod_by_file_id[fid] = _fomod_choices_from_collection(choices)

        # Sort the mods list by topo position (0 = highest priority);
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
        # file_id → (DownloadResult, effective_game_domain) — domain is the one we actually used
        _dl_results: dict[int, tuple] = {}
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
            # Enderal can use Skyrim mods; Enderal SE can use Skyrim SE mods.
            # If we get 404, retry under the corresponding Skyrim game.
            _ENDERAL_FALLBACKS = {"enderal": "skyrim", "enderalspecialedition": "skyrimspecialedition"}
            result = None
            effective_domain = self._game_domain
            try:
                result = downloader.download_file(
                    game_domain=self._game_domain,
                    mod_id=mod.mod_id,
                    file_id=mod.file_id,
                    progress_cb=_progress_cb,
                    cancel=dl_cancel,
                    known_file_name=mod.file_name or "",
                    expected_size_bytes=getattr(mod, "size_bytes", 0) or 0,
                    dest_dir=get_download_cache_dir(),
                )
                err = result.error or ""
                is_404 = "No Mod Found" in err or "No File found for mod" in err
                if not result.success and is_404:
                    fallback_domain = _ENDERAL_FALLBACKS.get(self._game_domain)
                    if fallback_domain:
                        self._log(
                            f"Collection install: mod {mod.mod_id} not found on {self._game_domain}, "
                            f"retrying under {fallback_domain}…"
                        )
                        result = downloader.download_file(
                            game_domain=fallback_domain,
                            mod_id=mod.mod_id,
                            file_id=mod.file_id,
                            progress_cb=_progress_cb,
                            cancel=dl_cancel,
                            known_file_name=mod.file_name or "",
                            expected_size_bytes=getattr(mod, "size_bytes", 0) or 0,
                            dest_dir=get_download_cache_dir(),
                        )
                        if result.success:
                            effective_domain = fallback_domain
            except Exception as exc:
                self._log(f"Collection install: download failed for '{mod.mod_name}': {exc}")

            # --- Hide popup ---
            if dl_cancel is not None and mod_panel is not None:
                try:
                    mod_panel.after(0, lambda ce=dl_cancel: mod_panel.hide_download_progress(cancel=ce))
                except Exception:
                    pass

            with _dl_lock:
                _dl_done += 1
                _dl_results[mod.file_id] = (result, effective_domain)
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

        # Archives >= 1 GB are extracted one at a time to avoid CPU/I/O thrashing
        # when multiple large extractions run in parallel.
        _LARGE_ARCHIVE_BYTES = 1 * 1024**3
        _large_archive_semaphore = threading.Semaphore(1)

        # Count uses per physical archive path so we only delete it after the
        # last consumer finishes.  Two separate collection entries can reference
        # the same physical archive (same file_id, or different file_ids whose
        # cached-archive lookup resolved to the same file on disk).
        _archive_use_count: dict[str, int] = {}
        for _m in to_download:
            _entry = _dl_results.get(_m.file_id) if _m.file_id else None
            _r = _entry[0] if isinstance(_entry, tuple) else _entry
            if _r and _r.success and _r.file_path:
                _key = str(_r.file_path)
                _archive_use_count[_key] = _archive_use_count.get(_key, 0) + 1

        _install_lock = threading.Lock()
        _install_counters = {"installed": 0, "skipped": 0, "done": 0}
        _install_results: dict[int, str] = {}  # file_id → installed folder name

        def _install_one(mod):
            _entry = _dl_results.get(mod.file_id)
            result, effective_domain = _entry if isinstance(_entry, tuple) else (_entry, self._game_domain)
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
                    game_domain=effective_domain,
                    mod_id=mod.mod_id,
                    file_id=mod.file_id,
                    archive_name=mod.file_name or "",
                )
                _pmeta.nexus_name = mod.mod_name or ""
                _pmeta.author = mod.mod_author or ""
                _pmeta.version = mod.version or ""
            except Exception:
                _pmeta = None

            # Preferred folder name: logicalFilename from collection.json is
            # the most specific (e.g. "Inventory Interface Information Injector
            # - Alchemy Fix"), then schema name, then Nexus mod page name.
            # This avoids two mods from the same page being stripped to the
            # same folder name by _suggest_mod_names.
            _logical = schema_file_id_to_logical.get(mod.file_id, "") or ""
            _schema_name = schema_pos_to_name.get(
                schema_file_id_to_pos.get(mod.file_id, -1), "") or ""
            _preferred = _logical or _schema_name or mod.mod_name or ""

            # Serialize extraction of large archives (>=1GB) to avoid system slowdown.
            _archive_size = 0
            try:
                _archive_size = os.path.getsize(archive_path)
            except OSError:
                pass
            _large_sem = _large_archive_semaphore if _archive_size >= _LARGE_ARCHIVE_BYTES else None
            if _large_sem is not None:
                _large_sem.acquire()
            try:
                folder_name = install_mod_from_archive(
                    archive_path, self, self._log, self._game,
                    fomod_auto_selections=auto_fomod,
                    prebuilt_meta=_pmeta,
                    profile_dir=profile_dir,
                    headless=True,
                    preferred_name=_preferred,
                    skip_index_update=True,
                    overwrite_existing=overwrite_existing,
                )
            finally:
                if _large_sem is not None:
                    _large_sem.release()

            with _install_lock:
                if folder_name:
                    _install_results[mod.file_id] = folder_name
                    _install_counters["installed"] += 1
                else:
                    _install_counters["skipped"] += 1
                _install_counters["done"] += 1
                done_so_far = _install_counters["done"]

                # Delete archive and .fileid sidecar once all consumers of this path are done.
                if archive_path in _archive_use_count:
                    _archive_use_count[archive_path] -= 1
                    if _archive_use_count[archive_path] == 0:
                        try:
                            delete_archive_and_sidecar(Path(archive_path))
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
            # will still serialize correctly via the _interactive_dialog_lock — running
            # small non-interactive mods first means the FOMOD prompts typically
            # appear after the bulk of parallel work is already done.
            _to_install = sorted(to_download, key=lambda m: getattr(m, "size_bytes", 0) or 0)
            with _cf.ThreadPoolExecutor(max_workers=_INSTALL_WORKERS) as _install_pool:
                list(_install_pool.map(_install_one, _to_install))

        installed += _install_counters["installed"]
        skipped  += _install_counters["skipped"]

        # Rebuild the mod index once for all newly installed mods rather than
        # updating it per-mod inside the workers (which caused lock contention).
        if _install_counters["installed"] > 0:
            try:
                self._log("Updating mod index…")
                _idx_path = profile_dir / "modindex.bin"
                rebuild_mod_index(
                    _idx_path,
                    self._game.get_effective_mod_staging_path(),
                    strip_prefixes=set(getattr(self._game, "strip_prefixes", None) or []),
                    allowed_extensions=set(getattr(self._game, "install_extensions", None) or []),
                    root_deploy_folders=set(getattr(self._game, "root_deploy_folders", None) or []),
                    normalize_folder_case=getattr(self._game, "normalize_folder_case", True),
                )
            except Exception as _idx_exc:
                self._log(f"Mod index rebuild skipped: {_idx_exc}")

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
        # Skipped when appending into an existing profile — the user's
        # existing load order is preserved; new mods are added by
        # install_mod_from_archive via ensure_mod_preserving_position.
        # ------------------------------------------------------------------
        if overwrite_existing is None:
            install_order.sort(key=lambda x: x[0])
            modlist_entries = [
                ModEntry(name=folder, enabled=True, locked=False)
                for _, folder in install_order
            ]
            if modlist_entries:
                try:
                    write_modlist(modlist_path, modlist_entries)
                    self._log(f"Collection install: wrote modlist.txt with {len(modlist_entries)} entries")
                except Exception as exc:
                    self._log(f"Collection install: failed to write modlist.txt: {exc}")

        # ------------------------------------------------------------------
        # Step 4: Write plugins.txt from collection.json if available
        # Also skipped when appending — existing plugin order is preserved.
        # ------------------------------------------------------------------
        schema_plugins: list[dict] = collection_schema.get("plugins", [])
        if schema_plugins and overwrite_existing is None:
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
        self._update_open_missing_btn_visibility()

    # ------------------------------------------------------------------
    # Reset load order
    # ------------------------------------------------------------------
    def _get_profile_dir(self) -> "Path | None":
        """Return the profile directory for this collection, or None if it doesn't exist."""
        if self._profile_dir_override is not None and self._profile_dir_override.is_dir():
            return self._profile_dir_override
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

    def _update_open_missing_btn_visibility(self):
        """Show 'Open Missing on Nexus' only when collection is installed and has missing mods."""
        if not hasattr(self, "_open_missing_btn") or self._open_missing_btn is None:
            return
        try:
            if self._get_profile_dir() is None:
                self._open_missing_btn.pack_forget()
                return
            missing_mod_ids = self._get_missing_mod_ids()
            if missing_mod_ids:
                self._open_missing_btn.pack(side="right", padx=(10, 0), pady=6)
            else:
                self._open_missing_btn.pack_forget()
        except Exception:
            self._open_missing_btn.pack_forget()

    def _get_missing_mod_ids(self) -> set[int]:
        """Return mod_ids of collection mods that are not installed (deduped)."""
        installed_names, file_id_to_folder = self._get_installed_mod_info()
        if installed_names is None:
            return set()
        missing: set[int] = set()
        for mod in getattr(self, "_loaded_mods", []) or []:
            if mod.mod_id <= 0:
                continue
            is_installed = False
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
            if not is_installed:
                missing.add(mod.mod_id)
        return missing

    def _on_open_missing_on_nexus(self):
        """Open Nexus pages for all mods in the collection that are not installed."""
        missing = self._get_missing_mod_ids()
        if not missing:
            return
        for mod_id in sorted(missing):
            url = f"https://www.nexusmods.com/{self._game_domain}/mods/{mod_id}"
            open_url(url)

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

            # Save manifest to profile dir for inspection
            try:
                import json as _json
                manifest_path = profile_dir / "collection.json"
                manifest_path.write_text(_json.dumps(cj, indent=2), encoding="utf-8")
                self._log(f"Saved collection manifest to {manifest_path}")
            except Exception as _exc:
                self._log(f"Could not save manifest: {_exc}")

            # Build file_id → priority position map respecting modRules
            fid_to_pos: dict = _topo_sort_collection(
                cj.get("mods", []), cj.get("modRules", [])
            )

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
            # Position 0 = highest priority → first in modlist.txt
            modlist_entries = [
                ModEntry(name=name, enabled=True, locked=False)
                for _, name in ordered
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
        initial_slug: Optional[str] = None,
        initial_game_domain: Optional[str] = None,
    ):
        super().__init__(parent, bg=BG_DEEP)
        self._game_domain = game_domain
        self._api = api
        self._game = game
        self._app_root = app_root or parent.winfo_toplevel()
        self._log = log_fn or (lambda msg: None)
        self._on_close = on_close
        self._initial_slug = initial_slug
        self._initial_game_domain = (initial_game_domain or game_domain).lower()

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
        if initial_slug:
            self.after(150, self._open_initial_collection)

    # ------------------------------------------------------------------
    # Build UI
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # URL parsing helper
    # ------------------------------------------------------------------

    def _open_initial_collection(self):
        """Open the collection specified by initial_slug (from nxm:// link)."""
        slug = self._initial_slug
        if not slug:
            return
        self._initial_slug = None  # only once
        from Nexus.nexus_api import NexusCollection
        domain = self._initial_game_domain or self._game_domain
        col = NexusCollection(slug=slug, name=slug, game_domain=domain)
        self._open_detail(col)

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
        toolbar = tk.Frame(self, bg=BG_HEADER, height=scaled(28))
        toolbar.grid(row=0, column=0, sticky="ew")
        toolbar.grid_propagate(False)

        # Close button — top-right, returns to modlist
        ctk.CTkButton(
            toolbar, text="✕ Close", width=scaled(72), height=scaled(26),
            fg_color="#b33a3a", hover_color="#c94848", text_color="white",
            font=FONT_HEADER, command=self._do_close,
        ).pack(side="right", padx=(4, 8), pady=2)

        self._prev_btn = ctk.CTkButton(
            toolbar, text="← Prev", width=scaled(70), height=scaled(26),
            fg_color="#c37800", hover_color="#e28b00", text_color="white",
            font=FONT_HEADER, command=self._go_prev_page,
            state="disabled",
        )
        self._prev_btn.pack(side="left", padx=(8, 4), pady=2)

        self._next_btn = ctk.CTkButton(
            toolbar, text="Next →", width=scaled(52), height=scaled(26),
            fg_color="#c37800", hover_color="#e28b00", text_color="white",
            font=FONT_HEADER, command=self._go_next_page,
            state="disabled",
        )
        self._next_btn.pack(side="left", padx=4, pady=2)

        self._open_current_btn = ctk.CTkButton(
            toolbar, text="Open Current", width=scaled(95), height=scaled(26),
            fg_color=ACCENT, hover_color=ACCENT_HOV, text_color="white",
            font=FONT_HEADER, command=self._open_current_collection,
        )
        # Only show if current profile has a collection URL
        self._open_current_url: str | None = None
        self._update_open_current_visibility()

        self._url_toggle_btn = ctk.CTkButton(
            toolbar, text="Open URL…", width=scaled(90), height=scaled(26),
            fg_color=ACCENT, hover_color=ACCENT_HOV, text_color="white",
            font=FONT_HEADER, command=self._toggle_url_bar,
        )
        self._url_toggle_btn.pack(side="left", padx=4, pady=2)

        self._status_label = ctk.CTkLabel(
            toolbar, text="Loading collections…", anchor="w",
            font=FONT_SMALL, text_color=TEXT_DIM, fg_color=BG_HEADER,
        )
        self._status_label.pack(side="left", padx=8, fill="x", expand=True)

        # URL bar (hidden by default — shown when "Open URL" button is pressed)
        self._url_bar = tk.Frame(self, bg=BG_HEADER, height=scaled(30))
        self._url_bar.grid(row=1, column=0, sticky="ew")
        self._url_bar.grid_propagate(False)
        self._url_bar.grid_remove()   # hidden until toggled

        ctk.CTkLabel(
            self._url_bar, text="Collection URL:",
            font=FONT_SMALL, text_color=TEXT_DIM, fg_color=BG_HEADER,
        ).pack(side="left", padx=(8, 4), pady=4)

        self._url_var = tk.StringVar()
        self._url_entry = ctk.CTkEntry(
            self._url_bar,
            textvariable=self._url_var,
            fg_color=BG_ROW, text_color=TEXT_MAIN,
            font=FONT_SMALL, height=scaled(26),
            border_width=0,
        )
        self._url_entry.pack(side="left", fill="x", expand=True, pady=4)
        self._url_entry.bind("<Return>", lambda _e: self._go_from_url())
        self._url_entry.bind(
            "<Control-a>",
            lambda _e: (self._url_entry.select_range(0, "end"), "break")[-1],
        )
        self._url_entry.bind("<Escape>", lambda _e: self._toggle_url_bar())

        ctk.CTkButton(
            self._url_bar, text="Go", width=scaled(40), height=scaled(26),
            fg_color=ACCENT, hover_color=ACCENT_HOV, text_color="white",
            font=FONT_HEADER, command=self._go_from_url,
        ).pack(side="left", padx=4, pady=4)

        ctk.CTkButton(
            self._url_bar, text="✕", width=scaled(32), height=scaled(26),
            fg_color="#b33a3a", hover_color="#c94848", text_color="white",
            font=FONT_HEADER, command=self._toggle_url_bar,
        ).pack(side="left", padx=(0, 8), pady=4)

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
        self._canvas.bind("<Map>", self._on_canvas_map)
        for w in (self._canvas, self._inner):
            w.bind("<Button-4>",   lambda e: self._scroll(-80))
            w.bind("<Button-5>",   lambda e: self._scroll(80))
            w.bind("<MouseWheel>", self._on_mousewheel)

        # Search bar
        search_bar = tk.Frame(self, bg=BG_HEADER, height=scaled(30))
        search_bar.grid(row=3, column=0, sticky="ew")
        search_bar.grid_propagate(False)

        ctk.CTkLabel(
            search_bar, text="Search:",
            font=FONT_SMALL, text_color=TEXT_DIM, fg_color=BG_HEADER,
        ).pack(side="left", padx=(8, 4), pady=4)

        self._search_var = tk.StringVar()
        self._search_entry = ctk.CTkEntry(
            search_bar,
            textvariable=self._search_var,
            fg_color=BG_ROW, text_color=TEXT_MAIN,
            font=FONT_SMALL, height=scaled(26),
            border_width=0,
        )
        self._search_entry.pack(side="left", fill="x", expand=True, pady=4, padx=(0, 4))
        self._search_entry.bind("<Return>", lambda _e: self._do_search())
        self._search_entry.bind(
            "<Control-a>",
            lambda _e: (self._search_entry.select_range(0, "end"), "break")[-1],
        )

        self._search_btn = ctk.CTkButton(
            search_bar, text="Search", width=scaled(64), height=scaled(26),
            fg_color=ACCENT, hover_color=ACCENT_HOV, text_color="white",
            font=FONT_HEADER, command=self._do_search,
        )
        self._search_btn.pack(side="left", padx=2, pady=4)

        self._clear_btn = ctk.CTkButton(
            search_bar, text="✕", width=scaled(32), height=scaled(26),
            fg_color="#b33a3a", hover_color="#c94848", text_color="white",
            font=FONT_HEADER, command=self._clear_search,
        )
        self._clear_btn.pack(side="left", padx=(0, 8), pady=4)

    def _do_close(self):
        """Close the collections panel and return to the modlist."""
        if self._on_close:
            self._on_close()
        else:
            self.place_forget()
            self.destroy()

    # ------------------------------------------------------------------
    # Open from URL / Open Current
    # ------------------------------------------------------------------

    def _update_open_current_visibility(self):
        """Show 'Open Current' button only if the active profile has a collection URL."""
        self._open_current_url = None
        profile_dir = getattr(self._game, "_active_profile_dir", None) if self._game else None
        if profile_dir:
            self._open_current_url = get_collection_url_from_profile(profile_dir)
        if self._open_current_url:
            self._open_current_btn.pack(side="left", padx=4, pady=2)
        else:
            self._open_current_btn.pack_forget()

    def _open_current_collection(self):
        """Open the collection in the manager (detail view) for the currently selected profile."""
        if not self._open_current_url:
            return
        slug, url_domain = self._parse_collection_url(self._open_current_url)
        if not slug:
            return
        game_domain = url_domain or self._game_domain
        from Nexus.nexus_api import NexusCollection
        col = NexusCollection(slug=slug, name=slug, game_domain=game_domain)
        # Pass current profile dir so Reset Load Order button appears (profile name may differ from slug)
        profile_dir = getattr(self._game, "_active_profile_dir", None) if self._game else None
        self._open_detail(col, profile_dir=profile_dir)

    def _toggle_url_bar(self):
        """Show/hide the URL input bar."""
        if self._url_bar.winfo_ismapped():
            self._url_bar.grid_remove()
            self._url_toggle_btn.configure(fg_color=ACCENT, hover_color=ACCENT_HOV)
        else:
            self._url_bar.grid()
            self._url_toggle_btn.configure(fg_color=ACCENT_HOV, hover_color=ACCENT)
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
        self._url_toggle_btn.configure(fg_color=ACCENT, hover_color=ACCENT_HOV)

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
        self._canvas.configure(scrollregion=(
            0, 0, self._inner.winfo_reqwidth(), self._inner.winfo_reqheight(),
        ))

    def _on_canvas_configure(self, event):
        if hasattr(self, '_regrid_after_id') and self._regrid_after_id:
            self.after_cancel(self._regrid_after_id)
        self._regrid_after_id = self.after(50, self._schedule_regrid)

    def _on_canvas_map(self, _event=None):
        """Re-grid when canvas becomes visible (e.g. after restore from minimized)."""
        if self._regrid_after_id:
            self.after_cancel(self._regrid_after_id)
        self._regrid_after_id = self.after(150, self._schedule_regrid)

    def _schedule_regrid(self):
        self._regrid_after_id = None
        self._canvas.update_idletasks()
        self._regrid_cards()

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

    def _open_detail(self, collection, profile_dir=None):
        self._close_detail()
        panel = CollectionDetailDialog(
            self, collection=collection,
            game_domain=self._game_domain, api=self._api,
            game=self._game, app_root=self._app_root, log_fn=self._log,
            on_close=self._close_detail,
            profile_dir=profile_dir,
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
        coll_w = scaled(_COLL_W)
        col_gap = scaled(4)  # gap between columns
        slot_w = coll_w + col_gap * 2
        canvas_w = self._canvas.winfo_width() or (_COLL_COLS * slot_w)
        self._cols = max(1, canvas_w // slot_w)

        # Inner frame = content width; center it in canvas by positioning the window
        content_w = self._cols * slot_w
        self._canvas.itemconfig(self._inner_id, width=content_w)
        x_off = max(0, (canvas_w - content_w) // 2)
        self._canvas.coords(self._inner_id, x_off, 0)

        # Grid: simple columns, no spacers (centering done via canvas window position)
        for c in range(self._cols):
            self._inner.grid_columnconfigure(c, weight=0, minsize=slot_w)

        _pad = col_gap
        for idx, c in enumerate(self._cards):
            col = idx % self._cols
            row = idx // self._cols
            c.card.grid(
                row=row, column=col,
                padx=(_pad, _pad),
                pady=scaled(CARD_PAD),
                sticky="n",
            )

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
