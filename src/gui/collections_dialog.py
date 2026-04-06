"""
collections_dialog.py
Browse Nexus Mods Collections for the currently selected game via GraphQL.

Opens as a standalone Toplevel window.  Displays 20 collections per page,
sorted by most downloaded by default.  Includes a search bar to filter
by name, and Prev / Next page navigation.
"""

from __future__ import annotations

import os
import queue as _queue_mod
import re
import threading
import tkinter as tk
import tkinter.messagebox
import tkinter.ttk as ttk
import webbrowser
from pathlib import Path
from typing import Callable, Optional

import customtkinter as ctk
from PIL import Image

from gui.ctk_components import CTkAlert, CTkLoader
from gui.dialogs import CollectionInstallModeDialog, CollectionContinueInstallDialog
from gui.game_helpers import (
    _create_profile,
    _profiles_for_game,
    _vanilla_plugins_for_game,
    find_profile_with_collection_url,
    get_collection_url_from_profile,
    save_collection_url_to_profile,
)
from Utils.profile_state import (
    read_collection_optional_skipped,
    write_collection_optional_skipped,
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
from Utils.plugins import PluginEntry, write_plugins, write_loadorder
from LOOT.loot_sorter import sort_plugins as _loot_sort, is_available as _loot_available

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

# ---------------------------------------------------------------------------
# Active-install registry — survives panel close/reopen
# ---------------------------------------------------------------------------
# Key: collection slug (str)
# Value: {
#   "status":           str,          # latest status text
#   "installed_fids":   set[int],     # file_ids successfully installed so far
#   "done":             bool,         # True once _run_install returns
# }
_ACTIVE_INSTALLS: dict[str, dict] = {}
# Paused installs: slug → {"cancel": threading.Event, "pause": threading.Event}
_PAUSED_INSTALLS: dict[str, dict] = {}

# DEBUG: set to True to force the non-premium manual-download flow regardless of
# the user's actual premium status. Remove or set to False before release.
_DEBUG_FORCE_MANUAL_INSTALL: bool = False


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

    # Reverse so that mods[-1] (last installed = highest priority in collection.json)
    # gets position 0 → top of modlist.txt (highest priority in the manager).
    # Without this, mods[0] (lowest priority) would incorrectly end up at the top.
    fid_order = list(reversed(fid_order))

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
    for step_idx, step in enumerate(choices.get("options", [])):
        groups: dict = {}
        for group in step.get("groups", []):
            group_name = group.get("name", "")
            plugin_names = [c["name"] for c in group.get("choices", []) if c.get("name")]
            if plugin_names:
                groups[group_name] = plugin_names
        if groups:
            result[str(step_idx)] = groups
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

    def load_image_async(self, url: str, cache: dict, loading: set, root: tk.Widget, on_done=None):
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

                def _done():
                    self._apply_image(photo)
                    if on_done is not None:
                        on_done()

                root.after(0, _done)
            except Exception:
                if on_done is not None:
                    root.after(0, on_done)
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
            tw.withdraw()
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
            tw.deiconify()
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

    def __init__(self, parent, optional_mods: list, on_done=None, pre_skipped_fids: "set[int] | None" = None):
        super().__init__(parent, fg_color=BG_DEEP, corner_radius=0)
        self.result = None
        self._optional_mods = optional_mods
        self._vars: dict[int, tk.BooleanVar] = {}
        self._on_done = on_done or (lambda p: None)
        _pre_skipped = pre_skipped_fids or set()

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
            var = tk.BooleanVar(value=mod.file_id not in _pre_skipped)
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

    def __init__(self, parent, collection, game_domain: str, api, game=None, app_root=None, log_fn=None, on_close=None, profile_dir=None, revision_number=None, local_manifest_path=None):
        super().__init__(parent, bg=BG_DEEP)
        self._collection = collection
        self._game_domain = game_domain
        self._api = api
        self._game = game
        self._app_root = app_root
        self._log = log_fn or (lambda *a: None)
        self._on_close = on_close or self.destroy
        self._profile_dir_override = profile_dir  # when set, use instead of deriving from collection name
        self._local_manifest_path = local_manifest_path  # when set, skip API fetch and load from file

        self._name_var = tk.StringVar(value=collection.name or collection.slug or "Collection")
        self._size_var = tk.StringVar(value="Loading\u2026")
        self._status_var = tk.StringVar(value="Fetching mod list\u2026")
        self._loaded_mods: list = []
        self._download_link_path: str = ""
        self._schema_order: dict = {}
        self._revision_number: int | None = revision_number  # None = latest
        self._revisions_list: list[dict] = []  # [{revisionNumber, revisionStatus}, ...]

        self._reset_btn = None  # created in _build_ui; shown only when profile exists
        self._offsite_mods: list[tuple[str, str]] = []  # (name, url) from collection.json
        self._offsite_frame: tk.Frame | None = None  # created in _build_ui
        self._revision_btn: ctk.CTkButton | None = None  # created in _build_ui
        self._revision_var = tk.StringVar(value="Loading\u2026")
        self._revision_popup: tk.Toplevel | None = None
        self._file_id_to_tree_iid: dict[int, str] = {}  # populated by _populate; used to green rows live
        self._install_poll_id: str | None = None  # after() id for install-progress polling

        # Premium status — determined in _worker(); controls download flow.
        self._nexus_is_premium: bool = False

        # Pre-populate collection schema cache from disk if available
        self._collection_schema_cache: dict = {}
        pd = self._get_profile_dir()
        if pd is not None:
            _manifest = pd / "collection.json"
            if _manifest.is_file():
                try:
                    import json as _json
                    self._collection_schema_cache = _json.loads(_manifest.read_text(encoding="utf-8"))
                    self._log("Loaded cached collection.json from profile")
                except Exception:
                    pass

        self._build_ui()
        if self._local_manifest_path:
            self.after(50, self._fetch_from_local_manifest)
        else:
            self._fetch()
        self.after(100, lambda: (self._update_reset_btn_visibility(), self._update_open_missing_btn_visibility(), self._update_install_btn_state()))
        # Reconnect to any install that started before this panel was (re)opened
        self.after(200, self._maybe_reconnect_install)

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

        # --- Revision picker ---
        rev_frame = tk.Frame(hdr, bg=BG_HEADER)
        rev_frame.pack(side="right", padx=(0, 8))
        tk.Label(
            rev_frame, text="Revision:",
            bg=BG_HEADER, fg=TEXT_DIM,
            font=font_sized_px("Segoe UI", 10),
        ).pack(side="left", padx=(0, 4))
        self._revision_btn = ctk.CTkButton(
            rev_frame,
            textvariable=self._revision_var,
            state="disabled",
            width=scaled(130),
            height=scaled(26),
            fg_color=BG_PANEL,
            hover_color=BG_HOVER,
            border_color=BG_SEP,
            border_width=1,
            text_color=TEXT_MAIN,
            text_color_disabled=TEXT_DIM,
            font=font_sized("Segoe UI", 10),
            anchor="w",
            command=self._open_revision_popup,
        )
        self._revision_btn.pack(side="left")

        # --- Status bar ---
        self._status_lbl = tk.Label(
            self, textvariable=self._status_var,
            bg=BG_DEEP, fg=TEXT_DIM,
            font=font_sized_px("Segoe UI", 12),
            anchor="w", bd=0, highlightthickness=0,
        )
        self._status_lbl.pack(fill="x", side="top", padx=10, pady=(4, 0))

        # --- Install progress bar (hidden until a collection install starts) ---
        self._install_progress_bar = ctk.CTkProgressBar(
            self,
            height=scaled(8),
            progress_color=ACCENT,
            fg_color=BG_PANEL,
            corner_radius=4,
        )
        self._install_progress_bar.set(0)
        # Do not pack yet — shown only during install

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
        self._tree.tag_configure("bundled", background="#1a2a3a", foreground="#7ab8e8")

        # --- Priority note ---
        self._priority_note = tk.Label(
            self, text="Order = author's install order  (↓ installed last = highest priority)",
            bg=BG_DEEP, fg=TEXT_DIM, font=font_sized_px("Segoe UI", 8), anchor="w",
        )
        self._priority_note.pack(fill="x", side="top", padx=10, pady=(0, 2))

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

        self._install_btn = ctk.CTkButton(
            ftr, text="Install Collection",
            height=scaled(30), fg_color="#2d7a2d", hover_color="#3a9e3a",
            text_color="#ffffff", font=font_sized("Segoe UI", 10, "bold"),
            border_width=0,
            command=self._on_install_collection,
        )
        self._install_btn.pack(side="right", padx=(10, 0), pady=6)

        ctk.CTkButton(
            ftr, text="Open on Nexus",
            height=scaled(30), fg_color="#3a5a8a", hover_color="#4a70aa",
            text_color="#ffffff", font=font_sized("Segoe UI", 10),
            border_width=0,
            command=self._on_open_on_nexus,
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

        # Off-site mods panel — inserted between priority note and footer at pack time
        self._offsite_frame = tk.Frame(self, bg=BG_DEEP, bd=0, highlightthickness=0)
        # Not packed yet — shown by _update_offsite_panel() when browse/direct mods exist

    # ------------------------------------------------------------------
    # Mod-list fetch
    # ------------------------------------------------------------------
    def _fetch(self):
        threading.Thread(target=self._worker, daemon=True).start()

    def _fetch_from_local_manifest(self):
        """Populate the detail panel from a local manifest.json file (no API needed)."""
        import json as _json
        from Nexus.nexus_api import NexusCollectionMod as _NCM
        try:
            manifest_path = self._local_manifest_path
            cj = _json.loads(open(manifest_path, encoding="utf-8").read())
        except Exception as exc:
            self._status_var.set(f"Failed to load manifest: {exc}")
            return

        self._collection_schema_cache = cj

        schema_mods: list[dict] = cj.get("mods", [])
        mods: list = []
        total_size = 0
        schema_order: dict[int, int] = {}
        offsite: list[tuple[str, str]] = []

        for pos, m in enumerate(schema_mods):
            src = m.get("source") or {}
            src_type = (src.get("type") or "nexus").lower()
            mod_name = m.get("name") or ""
            fid_raw = src.get("fileId")
            fid = int(fid_raw) if fid_raw is not None else 0
            mid_raw = src.get("modId")
            mid = int(mid_raw) if mid_raw is not None else 0
            file_size = int(src.get("fileSize") or 0)
            total_size += file_size
            if fid:
                schema_order[fid] = pos

            if src_type in ("browse", "direct"):
                url = src.get("url") or src.get("fileUrl") or ""
                if url:
                    offsite.append((mod_name, url))
                continue
            if src_type == "bundle":
                mods.append(_NCM(
                    mod_name=mod_name,
                    file_name=src.get("fileExpression") or mod_name,
                    source_type="bundle",
                ))
                continue

            mods.append(_NCM(
                mod_id=mid,
                file_id=fid,
                mod_name=mod_name,
                file_name=src.get("logicalFilename") or src.get("fileExpression") or mod_name,
                size_bytes=file_size,
                optional=bool(m.get("optional", False)),
                source_type="nexus",
            ))

        self._offsite_mods = offsite
        try:
            self.after(0, self._update_offsite_panel)
        except Exception:
            pass

        try:
            _user = self._api.validate()
            self._nexus_is_premium = bool(_user.is_premium)
        except Exception:
            self._nexus_is_premium = False

        col_name = cj.get("info", {}).get("name") or self._collection.name or "Local Manifest"
        self._collection.name = col_name
        self.after(0, lambda: self._populate(
            col_name, total_size, len(mods), mods, "", schema_order, None))

    def _open_revision_popup(self):
        """Open a scrollable popup listing all known revisions."""
        if not self._revisions_list or self._revision_btn is None:
            return
        # Close any existing popup first
        if self._revision_popup is not None:
            try:
                self._revision_popup.destroy()
            except Exception:
                pass
            self._revision_popup = None

        labels = self._revision_labels()
        btn = self._revision_btn
        x = btn.winfo_rootx()
        y = btn.winfo_rooty() + btn.winfo_height()
        w = btn.winfo_width()

        ROW_H = scaled(22)
        MAX_VISIBLE = 10
        visible = min(len(labels), MAX_VISIBLE)

        popup = tk.Toplevel(self)
        popup.overrideredirect(True)
        popup.configure(bg=BORDER)
        popup.geometry(f"{w}x{visible * ROW_H + 2}+{x}+{y}")
        popup.lift()
        self._revision_popup = popup

        inner = tk.Frame(popup, bg=BG_PANEL, bd=0, highlightthickness=0)
        inner.pack(fill="both", expand=True, padx=1, pady=1)

        sb = tk.Scrollbar(inner, orient="vertical", bg=BG_SEP, troughcolor=BG_DEEP,
                          activebackground=ACCENT, highlightthickness=0, bd=0, width=scaled(10))
        lb = tk.Listbox(
            inner,
            yscrollcommand=sb.set,
            bg=BG_PANEL, fg=TEXT_MAIN,
            selectbackground=ACCENT, selectforeground="#ffffff",
            activestyle="none",
            relief="flat", bd=0, highlightthickness=0,
            font=font_sized_px("Segoe UI", 10),
        )
        for lbl in labels:
            lb.insert("end", lbl)

        # Pre-select current
        current = self._revision_var.get()
        if current in labels:
            idx = labels.index(current)
            lb.selection_set(idx)
            lb.see(idx)

        sb.config(command=lb.yview)
        if len(labels) > MAX_VISIBLE:
            sb.pack(side="right", fill="y")
        lb.pack(side="left", fill="both", expand=True)

        def _pick(event=None):
            sel = lb.curselection()
            if not sel:
                return
            self._on_revision_selected(labels[sel[0]])
            popup.destroy()
            self._revision_popup = None

        def _dismiss(event=None):
            popup.destroy()
            self._revision_popup = None

        lb.bind("<ButtonRelease-1>", _pick)
        lb.bind("<Return>", _pick)
        popup.bind("<Escape>", _dismiss)
        popup.bind("<FocusOut>", lambda e: self.after(50, _maybe_dismiss))

        def _maybe_dismiss():
            try:
                if self._revision_popup is popup and not popup.focus_displayof():
                    popup.destroy()
                    self._revision_popup = None
            except Exception:
                pass

        lb.focus_set()

    def _revision_labels(self) -> list[str]:
        """Build the sorted label list from self._revisions_list."""
        sorted_revs = sorted(self._revisions_list,
                             key=lambda r: int(r.get("revisionNumber") or 0), reverse=True)
        labels = []
        for r in sorted_revs:
            num = r.get("revisionNumber", "?")
            status = r.get("revisionStatus", "")
            label = f"Rev {num}"
            if status and status.lower() != "published":
                label += f" ({status.lower()})"
            labels.append(label)
        return labels

    def _on_revision_selected(self, sel: str):
        # Values are like "Rev 3" or "Rev 3 (draft)" — extract leading integer
        try:
            rev_num = int(sel.split()[1])
        except (IndexError, ValueError):
            return
        if rev_num == self._revision_number:
            return
        self._revision_number = rev_num
        self._revision_var.set(sel)
        self._offsite_mods = []
        self._update_offsite_panel()
        self._status_var.set(f"Fetching revision {rev_num}\u2026")
        self._size_var.set("Loading\u2026")
        for iid in self._tree.get_children():
            self._tree.delete(iid)
        self._fetch()

    def _worker(self):
        try:
            name, total_size, mod_count, mods, dl_path, revisions = self._api.get_collection_detail(
                self._collection.slug, self._game_domain,
                revision_number=self._revision_number,
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

            # Determine premium status so install can branch later.
            try:
                _user = self._api.validate()
                self._nexus_is_premium = bool(_user.is_premium)
            except Exception:
                self._nexus_is_premium = False

            # Override the optional flag and mod name on each mod using
            # collection.json as the authoritative source — the GraphQL API
            # sometimes marks non-optional mods as optional and always uses the
            # mod *page* name which is shared across all files on that page,
            # making it impossible to distinguish e.g. a main file from an
            # optional patch when both come from the same page.
            if cj:
                _cj_info: dict[int, tuple[bool, str]] = {}  # file_id → (optional, name)
                for _cm in cj.get("mods", []):
                    _src = _cm.get("source") or {}
                    _fid = _src.get("fileId")
                    if _fid is not None:
                        _cj_info[int(_fid)] = (
                            bool(_cm.get("optional", False)),
                            _cm.get("name") or "",
                        )
                # Detect mod pages that contribute more than one file — for
                # those, the GraphQL mod_name is ambiguous and we should prefer
                # the collection.json name.
                _mod_id_counts: dict[int, int] = {}
                for _mod in mods:
                    if _mod.mod_id:
                        _mod_id_counts[_mod.mod_id] = _mod_id_counts.get(_mod.mod_id, 0) + 1
                for _mod in mods:
                    if _mod.file_id and _mod.file_id in _cj_info:
                        _opt, _cj_name = _cj_info[_mod.file_id]
                        _mod.optional = _opt
                        # Replace the generic page name with the specific
                        # collection.json name when the page has multiple files.
                        if _cj_name and _mod_id_counts.get(_mod.mod_id, 1) > 1:
                            _mod.mod_name = _cj_name

            # Extract off-site (browse/direct) and bundled mod entries from schema
            from Nexus.nexus_api import NexusCollectionMod as _NCM
            offsite: list[tuple[str, str]] = []
            bundled_mods: list[_NCM] = []
            for m in cj.get("mods", []):
                src = m.get("source") or {}
                src_type = (src.get("type") or "").lower()
                mod_name = m.get("name") or ""
                if src_type in ("browse", "direct"):
                    url = src.get("url") or src.get("fileUrl") or ""
                    if url:
                        offsite.append((mod_name, url))
                elif src_type == "bundle":
                    bundled_mods.append(_NCM(
                        mod_name=mod_name,
                        file_name=src.get("fileExpression") or mod_name,
                        source_type="bundle",
                    ))
            self._offsite_mods = offsite
            # Append bundled mods to the main list so they show in the treeview
            mods_with_bundled = list(mods) + bundled_mods
            try:
                self.after(0, self._update_offsite_panel)
            except Exception:
                pass

            # Update collection name from API (fixes slug-only placeholder when opened via URL/nxm)
            if name:
                self._collection.name = name
            try:
                display_name = name or self._collection.name or self._collection.slug or "Collection"
                self.after(0, lambda: self._populate(
                    display_name,
                    total_size, mod_count, mods_with_bundled, dl_path, schema_order, revisions))
            except Exception:
                pass
        except Exception as exc:
            self._log(f"CollectionDetail error: {exc}")
            try:
                self.after(0, lambda: self._status_var.set(f"Error: {exc}"))
            except Exception:
                pass

    def _populate(self, collection_name: str, total_size: int, mod_count: int, mods, dl_path: str = "", schema_order=None, revisions=None):
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

        # Populate revision picker (only update list when we have a fresh revisions list)
        if revisions and self._revision_btn is not None:
            self._revisions_list = revisions
            sorted_revs = sorted(revisions, key=lambda r: int(r.get("revisionNumber") or 0), reverse=True)
            labels = self._revision_labels()
            # Determine which revision is currently shown
            current_num = self._revision_number
            if current_num is None:
                published = [r for r in sorted_revs if (r.get("revisionStatus") or "").lower() == "published"]
                src = published if published else sorted_revs
                current_num = int(src[0].get("revisionNumber") or 0) if src else None
            target_label = ""
            if current_num is not None:
                target_label = next(
                    (lbl for lbl, r in zip(labels, sorted_revs)
                     if int(r.get("revisionNumber") or 0) == current_num),
                    labels[0] if labels else "",
                )
            self._revision_var.set(target_label or (labels[0] if labels else ""))
            self._revision_btn.configure(state="normal")

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

            is_bundled = getattr(mod, "source_type", "nexus") == "bundle"

            if is_bundled:
                row_tags = ("bundled",)
                file_label = "Bundled"
            elif is_installed:
                row_tags = ("installed", "unordered") if tag == "unordered" else ("installed",)
                file_label = mod.file_name
            else:
                row_tags = (tag,)
                file_label = mod.file_name

            iid = self._tree.insert(
                "", "end",
                values=(order_label, mod.mod_name, mod.mod_author, file_label,
                        _fmt_size(mod.size_bytes) if mod.size_bytes else "—", opt_mark),
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
        """Validate prerequisites then kick off the background install.
        Also handles Resume — clears paused state so the install restarts
        from scratch (already-installed mods are skipped automatically)."""
        slug = self._collection.slug or ""
        if slug:
            _PAUSED_INSTALLS.pop(slug, None)
        self._update_install_btn_state()
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

        # Show profile selection first so we can pre-populate optional mod choices
        # from any previously saved selection for that profile.
        self._continue_install_collection(app, list(mods), downloader)

    def _continue_install_collection(self, app, all_mods, downloader):
        """Show the profile-selection dialog, then handle optional mods after a profile
        is chosen (so saved choices can be pre-populated for existing profiles)."""
        if not self._game:
            return

        existing_profiles = _profiles_for_game(self._game.name)
        mod_panel = getattr(app, "_mod_panel", None)
        overlay_parent = mod_panel if mod_panel is not None else self

        # Check if this collection is already installed in an existing profile
        game_domain = getattr(self._game, "nexus_game_domain", None) or self._game_domain
        collection_url = f"https://www.nexusmods.com/{game_domain}/collections/{self._collection.slug}"
        if self._revision_number is not None:
            collection_url += f"/revisions/{self._revision_number}"
        existing_profile = find_profile_with_collection_url(self._game.name, collection_url)

        def _on_mode_chosen(result):
            try:
                overlay.destroy()
            except Exception:
                pass
            if result is None:
                self._status_var.set("Install cancelled.")
                return
            self._after_profile_selected(app, all_mods, downloader, result)

        if existing_profile:
            overlay = CollectionContinueInstallDialog(
                overlay_parent, existing_profile, _on_mode_chosen
            )
        else:
            overlay = CollectionInstallModeDialog(
                overlay_parent, existing_profiles, _on_mode_chosen
            )
        overlay.place(relx=0, rely=0, relwidth=1, relheight=1)
        overlay.lift()

    def _after_profile_selected(self, app, all_mods, downloader, mode_result):
        """Called after the profile dialog is dismissed. Shows the optional mods panel
        (if any), pre-populating choices saved from a previous install of this profile."""
        if not self._game:
            return

        # Ensure the optional flag is correct using collection.json as the
        # authoritative source.  The in-memory cache is preferred; if it is
        # absent (e.g. the archive download failed during _worker) we fall back
        # to the profile's saved copy so reinstalls still get the right flags.
        _cj_auth: dict = getattr(self, "_collection_schema_cache", None) or {}
        if not _cj_auth:
            # Try the profile's saved collection.json as a fallback.
            _mode, _pname, _ = mode_result
            if _pname and self._game:
                try:
                    import json as _json
                    _proot = self._game.get_profile_root()
                    _saved = _proot / "profiles" / _pname / "collection.json"
                    if _saved.is_file():
                        _cj_auth = _json.loads(_saved.read_text(encoding="utf-8"))
                except Exception:
                    pass
        if _cj_auth:
            _cj_info2: dict[int, tuple[bool, str]] = {}
            for _cm in _cj_auth.get("mods", []):
                _src = _cm.get("source") or {}
                _fid = _src.get("fileId")
                if _fid is not None:
                    _cj_info2[int(_fid)] = (
                        bool(_cm.get("optional", False)),
                        _cm.get("name") or "",
                    )
            _mod_id_counts2: dict[int, int] = {}
            for _mod in all_mods:
                if _mod.mod_id:
                    _mod_id_counts2[_mod.mod_id] = _mod_id_counts2.get(_mod.mod_id, 0) + 1
            for _mod in all_mods:
                if _mod.file_id and _mod.file_id in _cj_info2:
                    _opt, _cj_name = _cj_info2[_mod.file_id]
                    _mod.optional = _opt
                    if _cj_name and _mod_id_counts2.get(_mod.mod_id, 1) > 1:
                        _mod.mod_name = _cj_name

        optional_mods = [m for m in all_mods if m.optional]
        if optional_mods:
            # Load previously saved skipped fids for existing profiles so the user
            # doesn't have to re-select after a crash or reinstall.
            pre_skipped_fids: "set[int]" = set()
            mode, append_profile_name, _ = mode_result
            if mode in ("append", "continue") and append_profile_name:
                profile_root = self._game.get_profile_root()
                existing_profile_dir = profile_root / "profiles" / append_profile_name
                if existing_profile_dir.is_dir():
                    pre_skipped_fids = read_collection_optional_skipped(existing_profile_dir)

            show_fn = getattr(app, "show_optional_mods_panel", None)
            if show_fn:
                def _on_optional_done(panel):
                    if panel.result is None:
                        return
                    skipped_fids = panel.result or set()
                    skipped_mods = [
                        m for m in all_mods
                        if m.optional and m.file_id in skipped_fids
                    ]
                    mods_to_use = [
                        m for m in all_mods
                        if not m.optional or m.file_id not in skipped_fids
                    ]
                    self._finish_install_collection(
                        app, mods_to_use, downloader, mode_result, skipped_fids, skipped_mods
                    )
                show_fn(optional_mods, _on_optional_done, pre_skipped_fids=pre_skipped_fids)
                return
            # Fallback: no app overlay support — install all optional mods
            # (they are not skipped in headless/fallback mode)

        self._finish_install_collection(app, list(all_mods), downloader, mode_result, set(), [])

    def _finish_install_collection(self, app, mods, downloader, mode_result, skipped_fids: "set[int] | None" = None, skipped_mods: "list | None" = None):
        """Called after the install-mode overlay is dismissed."""
        if not self._game:
            return

        mode, append_profile_name, overwrite_existing = mode_result

        if mode == "continue":
            # Continue install into the profile that already has this collection.
            # Resolve profile dir like "append", but pass overwrite_existing=None
            # to _run_install so load order + plugins.txt are written fresh.
            profile_name = append_profile_name
            profile_root = self._game.get_profile_root()
            profile_dir = profile_root / "profiles" / profile_name
            if not profile_dir.is_dir():
                self._status_var.set(f"Profile '{profile_name}' not found.")
                return
            self._log(f"Collection install: continuing into existing profile '{profile_name}' at {profile_dir}")
            # Update the stored collection URL in case the revision changed
            game_domain = getattr(self._game, "nexus_game_domain", None) or self._game_domain
            collection_url = f"https://www.nexusmods.com/{game_domain}/collections/{self._collection.slug}"
            if self._revision_number is not None:
                collection_url += f"/revisions/{self._revision_number}"
            save_collection_url_to_profile(profile_dir, collection_url)
        elif mode == "new":
            # Sanitise collection name → profile name, append revision number
            raw = self._collection.name or self._collection.slug or "Collection"
            base = re.sub(r"[^\w\s\-]", "", raw).strip().replace(" ", "_") or "Collection"
            rev_num = self._revision_number
            if rev_num is None:
                # Parse from the revision button label (e.g. "Rev 13" or "Rev 13 (draft)")
                try:
                    rev_num = int(self._revision_var.get().split()[1])
                except (IndexError, ValueError):
                    rev_num = None
            if rev_num is not None:
                profile_name = f"{base}_Rev{rev_num}"[:64]
            else:
                profile_name = base[:64]

            self._status_var.set(f"Creating profile '{profile_name}'…")
            try:
                profile_dir = _create_profile(
                    self._game.name, profile_name, profile_specific_mods=True
                )
            except Exception as exc:
                self._status_var.set(f"Profile creation failed: {exc}")
                return

            self._log(f"Collection install: created profile '{profile_name}' at {profile_dir}")
            # Store collection URL in profile_state (profile_settings) for "Open Current" button
            game_domain = getattr(self._game, "nexus_game_domain", None) or self._game_domain
            collection_url = f"https://www.nexusmods.com/{game_domain}/collections/{self._collection.slug}"
            if self._revision_number is not None:
                collection_url += f"/revisions/{self._revision_number}"
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

        # Persist the optional mod choices so a crash-recovery or reinstall can
        # pre-populate the optional mods panel with the same selections.
        if skipped_fids is not None:
            try:
                write_collection_optional_skipped(profile_dir, skipped_fids)
            except Exception:
                pass

        self._status_var.set(f"Starting install of {len(mods)} mods into '{profile_name}'…")

        # Save the old profile dir so we can restore it after install
        old_profile = getattr(self._game, "_active_profile_dir", None)

        _install_args = (
            list(mods),
            self._download_link_path,
            profile_dir,
            old_profile,
            downloader,
            app,
            len(mods),
            None if mode in ("new", "continue") else overwrite_existing,
            skipped_fids,
            skipped_mods or [],
        )

        if self._nexus_is_premium and not _DEBUG_FORCE_MANUAL_INSTALL:
            # Premium: automated CDN download + parallel install
            self._show_install_overlay(len(mods), profile_name)
            threading.Thread(
                target=self._run_install,
                args=_install_args,
                daemon=True,
            ).start()
        else:
            # Non-premium: sequential manual-download overlay
            self._show_manual_install_overlay(len(mods), profile_name)
            threading.Thread(
                target=self._run_manual_install,
                args=_install_args,
                daemon=True,
            ).start()

    def _run_install(self, mods, download_link_path, profile_dir, old_profile, downloader, app, total, overwrite_existing: "bool | None" = None, skipped_fids: "set[int] | None" = None, skipped_mods: "list | None" = None):
        """Background thread: download then install each mod in collection-defined order.

        Load order is driven by ``collection.json`` from the collection archive:
        - ``mods`` array defines install order (index 0 = lowest priority,
          last entry = highest priority).
        - ``plugins`` array defines the exact ``plugins.txt`` order.
        Both are written after all mods are installed.
        """
        # Register this install so any panel reopened mid-install can reconnect.
        _slug = self._collection.slug or ""
        _install_state: dict = {"status": "", "installed_fids": set(), "done": False, "profile_dir": profile_dir}
        if _slug:
            _ACTIVE_INSTALLS[_slug] = _install_state

        def _set_status(msg: str) -> None:
            """Update status on both the registry and (if still alive) the panel."""
            _install_state["status"] = msg
            try:
                color = "#4caf50" if msg.startswith("Downloading") else TEXT_DIM
                self.after(0, lambda m=msg, c=color: (
                    self._status_var.set(m),
                    self._status_lbl.configure(fg=c),
                ))
            except Exception:
                pass

        def _set_progress(value: float | None) -> None:
            """Show/update/hide the install progress bar (0.0–1.0; None = hide)."""
            try:
                if value is None:
                    self.after(0, self._install_progress_bar.pack_forget)
                else:
                    def _show_and_set(v=value):
                        bar = self._install_progress_bar
                        if not bar.winfo_ismapped():
                            bar.pack(fill="x", side="top", padx=10, pady=(2, 0),
                                     after=self._status_lbl)
                        bar.set(v)
                        # Mirror to overlay bar if visible
                        _obar = getattr(self, "_install_overlay_bar", None)
                        if _obar is not None:
                            try:
                                _obar.set(v)
                            except Exception:
                                pass
                    self.after(0, _show_and_set)
            except Exception:
                pass

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
        # Always check the cache first — covers both Nexus collections (pre-fetched)
        # and local manifest installs (set by _fetch_from_local_manifest).
        cached = getattr(self, "_collection_schema_cache", None)
        if cached:
            collection_schema = cached
            self._log("Collection install: reusing cached collection.json")
        if download_link_path and not collection_schema:
            _set_status("Downloading collection manifest…")
            try:
                collection_schema = self._api.get_collection_archive_json(download_link_path)
                self._log(f"Collection install: parsed collection.json "
                          f"({len(collection_schema.get('mods', []))} mod entries, "
                          f"{len(collection_schema.get('plugins', []))} plugins)")
            except Exception as exc:
                self._log(f"Collection install: could not download collection.json: {exc} — "
                          "continuing with GraphQL order")

        # Save a copy of the manifest to the profile folder for inspection / future use
        if collection_schema:
            try:
                import json as _json
                manifest_path = profile_dir / "collection.json"
                manifest_path.write_text(_json.dumps(collection_schema, indent=2), encoding="utf-8")
                self._log(f"Collection install: saved manifest to {manifest_path}")
            except Exception as _exc:
                self._log(f"Collection install: could not save manifest: {_exc}")

        # Build a mapping from file_id → priority position (0 = highest priority)
        # respecting modRules before/after constraints via topological sort.
        schema_mods: list[dict] = collection_schema.get("mods", [])
        mod_rules: list[dict] = collection_schema.get("modRules", [])
        schema_file_id_to_pos: dict[int, int] = _topo_sort_collection(schema_mods, mod_rules)
        schema_pos_to_name: dict[int, str] = {}  # collection.json logical name
        schema_file_id_to_logical: dict[int, str] = {}  # file_id → logicalFilename
        schema_file_id_to_mod_id: dict[int, int] = {}   # file_id → mod_id from collection.json
        fomod_by_file_id: dict[int, dict] = {}   # file_id → saved_selections dict
        # First pass: collect raw logicalFilename values to detect duplicates
        _raw_logical: dict[int, str] = {}   # file_id → raw logicalFilename from source
        _raw_name: dict[int, str] = {}      # file_id → schema mod name
        for schema_mod in schema_mods:
            src = schema_mod.get("source") or {}
            fid = src.get("fileId")
            if fid is not None:
                fid = int(fid)
                _raw_logical[fid] = src.get("logicalFilename") or ""
                _raw_name[fid] = schema_mod.get("name") or ""
        # Count how many file_ids share each logicalFilename
        _logical_counts: dict[str, int] = {}
        for raw in _raw_logical.values():
            if raw:
                _logical_counts[raw] = _logical_counts.get(raw, 0) + 1

        for pos, schema_mod in enumerate(schema_mods):
            src = schema_mod.get("source") or {}
            fid = src.get("fileId")
            if fid is not None:
                fid = int(fid)
                topo_pos = schema_file_id_to_pos.get(fid, pos)
                schema_pos_to_name[topo_pos] = schema_mod.get("name") or ""
                raw_logical = _raw_logical.get(fid, "")
                schema_name = _raw_name.get(fid, "")
                # If multiple entries share the same logicalFilename, fall back to
                # the more specific schema name to avoid folder-name collisions
                # (e.g. "Capital Whiterun Expansion" shared by main mod + meshes patch).
                if raw_logical and _logical_counts.get(raw_logical, 0) > 1:
                    logical = schema_name or raw_logical
                else:
                    logical = raw_logical or schema_name
                schema_file_id_to_logical[fid] = logical
                mid = src.get("modId")
                if mid:
                    schema_file_id_to_mod_id[fid] = int(mid)
                choices = schema_mod.get("choices") or {}
                if choices.get("type") == "fomod":
                    fomod_by_file_id[fid] = _fomod_choices_from_collection(choices)
                elif choices.get("type") == "fomod_selections":
                    fomod_by_file_id[fid] = choices["selections"]

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

        # ------------------------------------------------------------------
        # Remove staging folders for unticked optional mods
        # ------------------------------------------------------------------
        if skipped_fids and skipped_mods:
            import shutil as _shutil_skip
            _removed_folders: list[str] = []
            for mod in skipped_mods:
                if not mod.file_id or mod.file_id not in skipped_fids:
                    continue

                # Match by file_id first (same as classify step)
                folder_name = already_installed_by_fid.get(mod.file_id, "")

                # Fallback: match by predicted folder name (same logic as classify)
                if not folder_name:
                    logical = schema_file_id_to_logical.get(mod.file_id, "") or ""
                    schema_name = schema_pos_to_name.get(
                        schema_file_id_to_pos.get(mod.file_id, -1), "") or ""
                    candidates: list[str] = []
                    name_sources = (logical, schema_name) if (logical or schema_name) else (mod.mod_name or "",)
                    for raw in name_sources:
                        if raw:
                            for s in _suggest_mod_names(raw):
                                if s and s not in candidates:
                                    candidates.append(s)
                    for candidate in candidates:
                        key = candidate.lower()
                        if key in staging_lower_map:
                            folder_name = staging_lower_map[key]
                            break

                if folder_name:
                    skip_dir = staging_path / folder_name
                    if skip_dir.is_dir():
                        self._log(f"Collection install: removing unticked optional mod '{folder_name}' (file_id={mod.file_id})")
                        try:
                            _shutil_skip.rmtree(skip_dir)
                            _removed_folders.append(folder_name)
                        except Exception as exc:
                            self._log(f"Collection install: failed to remove '{folder_name}': {exc}")

            # Batch-remove all deleted folders from modlist.txt
            if _removed_folders and modlist_path.is_file():
                try:
                    _removed_set = set(_removed_folders)
                    entries = read_modlist(modlist_path)
                    entries = [e for e in entries if e.name not in _removed_set]
                    write_modlist(modlist_path, entries)
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
                # Only use mod.mod_name as a fallback when we have no logical/schema
                # name — otherwise two mods from the same page (e.g. main mod + meshes
                # patch both named "Capital Whiterun Expansion" in GraphQL) would
                # incorrectly be treated as the same already-installed mod.
                name_sources = (logical, schema_name) if (logical or schema_name) else (mod.mod_name or "",)
                for raw in name_sources:
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
        # Step 2: Pipeline — download and install concurrently.
        # Downloads and installs run as a producer-consumer pipeline so
        # install workers begin extracting archives as soon as each
        # download finishes, rather than waiting for ALL downloads first.
        # A bounded queue provides back-pressure: when _PIPELINE_QUEUE_SIZE
        # archives are downloaded-but-not-yet-installed, download threads
        # block — preventing disk/memory exhaustion.
        # ------------------------------------------------------------------
        import concurrent.futures as _cf
        import queue as _queue
        from Utils.ui_config import load_collection_settings as _load_col_cfg
        _col_cfg = _load_col_cfg()

        _DL_WORKERS = _col_cfg["max_concurrent"]
        _INSTALL_WORKERS = 4
        _PIPELINE_QUEUE_SIZE = max(_INSTALL_WORKERS + 1, 5)
        _DONE_SENTINEL = None  # pushed once per install consumer to signal shutdown

        # file_id → (DownloadResult, effective_game_domain) — kept for post-install order tracking
        _dl_results: dict[int, tuple] = {}
        _dl_lock = threading.Lock()
        _dl_done = 0
        _dl_total = len(to_download)
        mod_panel = getattr(app, "_mod_panel", None)

        # --- Single collection-wide progress bar ---
        _to_download_fids = {getattr(m, "file_id", None) for m in to_download}
        _total_bytes = sum(getattr(m, "size_bytes", 0) or 0 for m in ordered_mods)
        _dl_bytes_done = sum(
            getattr(m, "size_bytes", 0) or 0
            for m in ordered_mods
            if getattr(m, "file_id", None) not in _to_download_fids
        )  # pre-credit already-installed/skipped mods
        _per_mod_prev: dict[int, int] = {}  # file_id → last reported bytes (for delta tracking)
        _col_cancel = threading.Event()
        _col_pause = threading.Event()   # set to pause after current items finish
        _dl_finished = threading.Event()   # set when all downloads are done
        _pipeline_finished = threading.Event()  # set when downloads AND installs are done

        # Register pause/cancel events so the UI button can trigger them
        if _slug:
            _ACTIVE_INSTALLS[_slug]["cancel"] = _col_cancel
            _ACTIVE_INSTALLS[_slug]["pause"] = _col_pause

        # Archives >= 1 GB are extracted with limited concurrency to avoid
        # excessive memory/I/O pressure, but allow 2 at a time since native
        # tools (7z/bsdtar) are more memory-efficient than py7zr.
        _LARGE_ARCHIVE_BYTES = 1 * 1024**3
        _large_archive_semaphore = threading.Semaphore(2)

        # Archive-use counting built incrementally as downloads complete.
        # Two mods can reference the same cached archive (same file_id, or
        # different file_ids whose cache lookup resolved to the same file).
        _archive_use_count: dict[str, int] = {}

        _install_lock = threading.Lock()
        _install_counters = {"installed": 0, "skipped": 0, "done": 0}
        _install_results: dict[int, str] = {}  # file_id → installed folder name

        # Bounded queue: download producers → install consumers.
        # Items are (mod, DownloadResult, effective_domain) or _DONE_SENTINEL.
        _install_queue: _queue.Queue = _queue.Queue(maxsize=_PIPELINE_QUEUE_SIZE)

        # ------------------------------------------------------------------
        # Download worker (producer) — pushes each result onto the queue
        # ------------------------------------------------------------------
        def _download_one(mod):
            nonlocal _dl_done, _dl_bytes_done

            # If paused or cancelled, skip this download (push None so install queue drains)
            if _col_pause.is_set() or _col_cancel.is_set():
                with _dl_lock:
                    _dl_done += 1
                _install_queue.put((mod, None, self._game_domain))
                return

            def _progress_cb(cur: int, tot: int, _fid=mod.file_id):
                nonlocal _dl_bytes_done
                with _dl_lock:
                    prev = _per_mod_prev.get(_fid, 0)
                    delta = max(cur - prev, 0)
                    _per_mod_prev[_fid] = cur
                    _dl_bytes_done += delta

            # Enderal can use Skyrim mods; Enderal SE can use Skyrim SE mods.
            _ENDERAL_FALLBACKS = {"enderal": "skyrim", "enderalspecialedition": "skyrimspecialedition"}
            result = None
            effective_domain = self._game_domain
            try:
                result = downloader.download_file(
                    game_domain=self._game_domain,
                    mod_id=mod.mod_id,
                    file_id=mod.file_id,
                    progress_cb=_progress_cb,
                    cancel=_col_cancel,
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
                            cancel=_col_cancel,
                            known_file_name=mod.file_name or "",
                            expected_size_bytes=getattr(mod, "size_bytes", 0) or 0,
                            dest_dir=get_download_cache_dir(),
                        )
                        if result.success:
                            effective_domain = fallback_domain
            except Exception as exc:
                self._log(f"Collection install: download failed for '{mod.mod_name}': {exc}")

            # If progress_cb was never called (cached archive skip), advance by full mod size
            mod_size = getattr(mod, "size_bytes", 0) or 0
            if mod_size > 0 and _per_mod_prev.get(mod.file_id, 0) == 0:
                _progress_cb(mod_size, mod_size)

            with _dl_lock:
                _dl_done += 1
                _dl_results[mod.file_id] = (result, effective_domain)
                done = _dl_done

            # Increment archive use count under _install_lock (same lock used
            # by consumers to decrement) to prevent a race when two mods
            # share the same cached archive path.
            with _install_lock:
                if result and result.success and result.file_path:
                    _akey = str(result.file_path)
                    _archive_use_count[_akey] = _archive_use_count.get(_akey, 0) + 1
                _inst_done = _install_counters["done"]
            _set_status(f"Downloaded {done}/{_dl_total}, installed {_inst_done}/{_dl_total}\u2026")

            # Push onto the bounded install queue (blocks if queue is full,
            # providing back-pressure so archives don't pile up on disk).
            _install_queue.put((mod, result, effective_domain))

        # ------------------------------------------------------------------
        # Install worker (consumer) — pulls from queue until sentinel
        # ------------------------------------------------------------------
        def _install_one(mod, result, effective_domain):
            """Install a single downloaded mod archive."""
            if _col_cancel.is_set() or _col_pause.is_set():
                with _install_lock:
                    _install_counters["skipped"] += 1
                    _install_counters["done"] += 1
                return

            if result is None or not result.success or not result.file_path:
                self._log(f"Collection install: download failed for '{mod.mod_name}'")
                with _install_lock:
                    _install_counters["skipped"] += 1
                    _install_counters["done"] += 1
                return

            archive_path = str(result.file_path)
            auto_fomod = fomod_by_file_id.get(mod.file_id)

            # Build prebuilt metadata so no extra API calls are needed.
            # Prefer the mod_id from collection.json (source.modId) over the
            # GraphQL API value, which can be wrong when Nexus associates a
            # file with the wrong mod page.
            try:
                _effective_mod_id = schema_file_id_to_mod_id.get(mod.file_id, 0) or mod.mod_id
                _pmeta = build_meta_from_download(
                    game_domain=effective_domain,
                    mod_id=_effective_mod_id,
                    file_id=mod.file_id,
                    archive_name=mod.file_name or "",
                )
                _pmeta.nexus_name = mod.mod_name or ""
                _pmeta.author = mod.mod_author or ""
                _pmeta.version = mod.version or ""
            except Exception:
                _pmeta = None

            # Preferred folder name: logicalFilename from collection.json is
            # the most specific, then schema name, then Nexus mod page name.
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

            # Update progress and mark row green — also write to registry for reconnect.
            with _dl_lock:
                dl_done_now = _dl_done
            _set_status(f"Downloaded {dl_done_now}/{_dl_total}, installed {done_so_far}/{_dl_total}\u2026")
            _set_progress(done_so_far / _dl_total if _dl_total else 1.0)
            if mod.file_id and folder_name:
                _install_state["installed_fids"].add(mod.file_id)
                try:
                    self.after(0, lambda fid=mod.file_id: self._mark_row_installed(fid))
                except Exception:
                    pass

        def _install_consumer():
            """Long-lived consumer thread: pull items from the queue and install."""
            while True:
                item = _install_queue.get()
                if item is _DONE_SENTINEL:
                    _install_queue.task_done()
                    break
                mod, result, effective_domain = item
                try:
                    _install_one(mod, result, effective_domain)
                except Exception as exc:
                    self._log(f"Collection install: unexpected error installing '{mod.mod_name}': {exc}")
                    with _install_lock:
                        _install_counters["skipped"] += 1
                        _install_counters["done"] += 1
                finally:
                    _install_queue.task_done()

        # ------------------------------------------------------------------
        # Launch the pipeline: install consumers first, then download producers.
        # ------------------------------------------------------------------
        if to_download:
            _set_status(f"Downloading & installing {_dl_total} mod(s)\u2026")
            _set_progress(0.0)
            _to_download_sorted = sorted(
                to_download,
                key=lambda m: getattr(m, "size_bytes", 0) or 0,
                reverse=(_col_cfg["download_order"] == "largest"),
            )

            # --- Show download progress inside the install overlay ---
            if _total_bytes > 0:
                tot_gb = _total_bytes / (1024 ** 3)
                lbl = f"Downloading & installing {_dl_total} mod(s)  ({tot_gb:.1f} GB)"
                _ol_ready = threading.Event()

                def _init_overlay_dl():
                    try:
                        self._show_overlay_download(lbl)
                    except Exception:
                        pass
                    finally:
                        _ol_ready.set()

                try:
                    self.after(0, _init_overlay_dl)
                except Exception:
                    _ol_ready.set()
                _ol_ready.wait(timeout=5)

                import time as _time_mod
                _speed_state = {"prev_bytes": 0, "prev_time": _time_mod.monotonic()}

                def _poll_overlay_dl():
                    if _dl_finished.is_set():
                        self._update_overlay_download(_total_bytes, _total_bytes, 0.0)
                        self.after(500, self._hide_overlay_download)
                        return
                    now = _time_mod.monotonic()
                    with _dl_lock:
                        agg = _dl_bytes_done
                    dt = now - _speed_state["prev_time"]
                    if dt >= 0.5:
                        speed = (agg - _speed_state["prev_bytes"]) / dt
                        _speed_state["prev_bytes"] = agg
                        _speed_state["prev_time"] = now
                        _speed_state["speed"] = speed
                    speed_mbs = _speed_state.get("speed", 0.0) / (1024 * 1024)
                    self._update_overlay_download(agg, _total_bytes, speed_mbs)
                    self.after(200, _poll_overlay_dl)

                try:
                    self.after(200, _poll_overlay_dl)
                except Exception:
                    pass

            # Start install consumer threads first so they're ready for work.
            _consumer_threads: list[threading.Thread] = []
            for _ci in range(_INSTALL_WORKERS):
                t = threading.Thread(target=_install_consumer, daemon=True,
                                     name=f"col-install-{_ci}")
                t.start()
                _consumer_threads.append(t)

            # Run download producers — each pushes results onto the queue as
            # they finish so install consumers start working immediately.
            with _cf.ThreadPoolExecutor(max_workers=_DL_WORKERS) as _pool:
                list(_pool.map(_download_one, _to_download_sorted))

            # All downloads done — dismiss the download progress bar.
            _dl_finished.set()

            # Signal each install consumer to shut down after draining the queue.
            for _ in range(_INSTALL_WORKERS):
                _install_queue.put(_DONE_SENTINEL)

            # Wait for all install consumers to finish.
            for t in _consumer_threads:
                t.join()

            _pipeline_finished.set()

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
        # Step 2c: Install bundled assets from the collection archive
        # ------------------------------------------------------------------
        bundle_schema_mods = [
            m for m in schema_mods
            if (m.get("source") or {}).get("type", "").lower() == "bundle"
        ]
        if bundle_schema_mods and download_link_path:
            import tempfile as _tf
            _set_status(f"Downloading collection archive for {len(bundle_schema_mods)} bundled mod(s)…")
            bundle_extract_dir = _tf.mkdtemp(prefix="amethyst_bundle_")
            try:
                cj_full = self._api.get_collection_archive_full(
                    download_link_path, bundle_extract_dir
                )
                if cj_full:
                    import os as _os
                    from pathlib import Path as _Path
                    for bm in bundle_schema_mods:
                        bm_name = bm.get("name") or ""
                        src = bm.get("source") or {}
                        file_expr = src.get("fileExpression") or bm_name
                        # Bundled files live at bundled/<fileExpression>/ or bundled/<name>/
                        bundle_subdir = _Path(bundle_extract_dir) / "bundled" / file_expr
                        if not bundle_subdir.is_dir():
                            bundle_subdir = _Path(bundle_extract_dir) / "bundled" / bm_name
                        if not bundle_subdir.is_dir():
                            self._log(f"Collection install: bundled asset '{bm_name}' not found in archive")
                            skipped += 1
                            continue

                        mod_name_clean = re.sub(r"[^\w\s\-]", "", bm_name).strip().replace(" ", "_") or file_expr
                        if mod_name_clean.lower() in {k.lower() for k in staging_lower_map}:
                            self._log(f"Collection install: bundled '{bm_name}' already installed — skipping")
                            existing = staging_lower_map.get(mod_name_clean.lower(), mod_name_clean)
                            install_order.append((-1, existing))
                            installed += 1
                            continue

                        _set_status(f"Installing bundled asset: {bm_name}…")
                        try:
                            import shutil as _shutil2
                            import configparser as _cpi
                            dest = staging_path / mod_name_clean
                            if dest.exists():
                                _shutil2.rmtree(dest)
                            _shutil2.copytree(str(bundle_subdir), str(dest))
                            # Write meta.ini so it's recognised as an installed mod
                            meta = dest / "meta.ini"
                            cp = _cpi.ConfigParser()
                            cp["General"] = {
                                "modname": bm_name,
                                "installationfile": file_expr,
                            }
                            with open(meta, "w", encoding="utf-8") as mf:
                                cp.write(mf)
                            # Bundled assets are patches — place at highest priority
                            # (index 0 in modlist = highest priority in this app).
                            install_order.append((-1, mod_name_clean))
                            installed += 1
                            self._log(f"Collection install: installed bundled asset '{bm_name}' → '{mod_name_clean}'")
                        except Exception as exc:
                            self._log(f"Collection install: failed to install bundled asset '{bm_name}': {exc}")
                            skipped += 1
            except Exception as exc:
                self._log(f"Collection install: error processing bundled assets: {exc}")
            finally:
                import shutil as _shutil
                try:
                    _shutil.rmtree(bundle_extract_dir, ignore_errors=True)
                except Exception:
                    pass

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
                    # Group bundle variants under locked separators.
                    # Bundle variants use the <bundle>__<variant> naming convention.
                    _bundle_map: dict[str, list[ModEntry]] = {}
                    _non_bundle: list[ModEntry] = []
                    for me in modlist_entries:
                        if "__" in me.name:
                            bname = me.name.split("__", 1)[0]
                            _bundle_map.setdefault(bname, []).append(me)
                        else:
                            _non_bundle.append(me)
                    # Rebuild: non-bundle entries first, then bundle blocks
                    final_entries: list[ModEntry] = list(_non_bundle)
                    for bname, variants in _bundle_map.items():
                        sep_name = f"{bname}_separator"
                        final_entries.append(
                            ModEntry(name=sep_name, enabled=True, locked=True, is_separator=True))
                        for v in variants:
                            v.locked = False
                            v.enabled = True
                            final_entries.append(v)
                    write_modlist(modlist_path, final_entries)
                    # Lock bundle separators
                    if _bundle_map:
                        from Utils.profile_state import read_separator_locks, write_separator_locks
                        _locks = read_separator_locks(profile_dir)
                        for bname in _bundle_map:
                            _locks[f"{bname}_separator"] = True
                        write_separator_locks(profile_dir, _locks)
                    self._log(f"Collection install: wrote modlist.txt with {len(final_entries)} entries")
                except Exception as exc:
                    self._log(f"Collection install: failed to write modlist.txt: {exc}")

        # ------------------------------------------------------------------
        # Step 4: Write plugins.txt / loadorder.txt from collection.json.
        # Also skipped when appending — existing plugin order is preserved.
        #
        # Strategy:
        #   1. Run LOOT over (vanilla + collection plugins) so libloot can
        #      pin vanilla ESMs/ESPs to their correct positions.
        #   2. Extract the vanilla prefix from LOOT's sorted result
        #      (entries that are NOT in the collection's plugin list).
        #   3. Apply the collection author's load order on top:
        #      final = vanilla_prefix (LOOT-ordered) + author's mod order.
        #   This mirrors exactly what "Reset Load Order" does at runtime.
        # ------------------------------------------------------------------
        schema_plugins: list[dict] = collection_schema.get("plugins", [])
        if schema_plugins and overwrite_existing is None:
            try:
                # Author's original plugin order from collection.json
                author_entries = [
                    PluginEntry(name=p.get("name", ""), enabled=p.get("enabled", True))
                    for p in schema_plugins if p.get("name", "")
                ]
                author_lower = {e.name.lower() for e in author_entries}

                # Always fetch vanilla map — needed for exclusion from plugins.txt
                vanilla_map = _vanilla_plugins_for_game(self._game)  # lower -> orig
                plugins_include_vanilla = getattr(self._game, "plugins_include_vanilla", False)
                vanilla_lower: set[str] = set() if plugins_include_vanilla else set(vanilla_map.keys())

                # Step 4a: LOOT sort to establish correct vanilla positions
                loot_vanilla_prefix: list[PluginEntry] = []
                loot_enabled = getattr(self._game, "loot_sort_enabled", False)
                if loot_enabled and _loot_available():
                    try:
                        _set_status("Running LOOT sort to order vanilla plugins…")
                        _ext_order = {".esm": 0, ".esp": 1, ".esl": 2}
                        vanilla_prepend = [
                            PluginEntry(name=orig, enabled=True)
                            for low, orig in sorted(
                                vanilla_map.items(),
                                key=lambda kv: (_ext_order.get(Path(kv[0]).suffix, 9), kv[0]),
                            )
                            if low not in author_lower
                        ]
                        all_entries = vanilla_prepend + author_entries
                        name_to_enabled = {e.name: e.enabled for e in all_entries}
                        loot_result = _loot_sort(
                            plugin_names=[e.name for e in all_entries],
                            enabled_set={e.name for e in all_entries if e.enabled},
                            game_name=self._game.name,
                            game_path=self._game.get_game_path(),
                            staging_root=self._game.get_effective_mod_staging_path(),
                            log_fn=self._log,
                            game_type_attr=getattr(self._game, "loot_game_type", ""),
                            game_id=getattr(self._game, "game_id", ""),
                            masterlist_url=getattr(self._game, "loot_masterlist_url", ""),
                            game_data_dir=(
                                self._game.get_vanilla_plugins_path()
                                if hasattr(self._game, "get_vanilla_plugins_path") else None
                            ),
                        )
                        # Extract LOOT-ordered vanilla entries (not in author's list)
                        loot_vanilla_prefix = [
                            PluginEntry(name=n, enabled=name_to_enabled.get(n, True))
                            for n in loot_result.sorted_names
                            if n.lower() not in author_lower
                        ]
                        self._log(
                            f"Collection install: LOOT sort placed {len(loot_vanilla_prefix)} vanilla plugin(s)."
                        )
                    except Exception as loot_exc:
                        self._log(f"Collection install: LOOT sort skipped — {loot_exc}")

                # Step 4b: Apply author's load order — vanilla prefix (LOOT) + author's mod order
                final_entries = loot_vanilla_prefix + author_entries
                star_prefix = getattr(self._game, "plugins_use_star_prefix", True)
                write_plugins(
                    plugins_path,
                    [e for e in final_entries if e.name.lower() not in vanilla_lower],
                    star_prefix=star_prefix,
                )
                write_loadorder(plugins_path.parent / "loadorder.txt", final_entries)
                self._log(
                    f"Collection install: wrote plugins.txt ({len(author_entries)} mod plugins, "
                    f"{len(loot_vanilla_prefix)} vanilla prefix)."
                )
            except Exception as exc:
                self._log(f"Collection install: failed to write plugins.txt: {exc}")

        # ------------------------------------------------------------------
        # Final reconciliation: ensure every mod in modlist.txt is enabled
        # and in collection-defined order.  This runs unconditionally so a
        # crash-restart (any mode) always ends in a clean, ordered state.
        # ------------------------------------------------------------------
        if install_order and modlist_path.is_file():
            try:
                _folder_to_key: dict[str, int] = {
                    folder: key for key, folder in install_order
                }
                _existing = read_modlist(modlist_path)
                # Enable every entry and sort by collection position.
                # Entries not in install_order (e.g. separators added by Step 3)
                # keep their relative position at the end.
                _known   = [e for e in _existing if e.name in _folder_to_key]
                _unknown = [e for e in _existing if e.name not in _folder_to_key]
                for e in _known:
                    e.enabled = True
                for e in _unknown:
                    if not e.is_separator:
                        e.enabled = True
                _known.sort(key=lambda e: _folder_to_key[e.name])
                _reconciled = _known + _unknown
                write_modlist(modlist_path, _reconciled)
                self._log(
                    f"Collection install: reconciled modlist.txt "
                    f"({len(_known)} ordered, {len(_unknown)} trailing)"
                )
            except Exception as exc:
                self._log(f"Collection install: reconcile modlist failed: {exc}")

        # Restore the original profile dir
        self._game.set_active_profile_dir(old_profile)

        # If cancelled, hand off to cleanup (runs on main thread via after()).
        if _col_cancel.is_set():
            _install_state["done"] = True
            _ACTIVE_INSTALLS.pop(_slug, None)
            try:
                self.after(0, lambda _pd=profile_dir: self._do_cancel_cleanup(_pd))
            except Exception:
                pass
            return

        # If paused (but not cancelled), register so the Resume button appears.
        if _col_pause.is_set():
            if _slug:
                _PAUSED_INSTALLS[_slug] = {"profile_dir": profile_dir}
            _install_state["status"] = f"Paused — {installed} installed so far."
            _install_state["done"] = True
            _ACTIVE_INSTALLS.pop(_slug, None)
            try:
                self.after(0, lambda: self._on_install_paused(installed, str(profile_dir.name)))
            except Exception:
                pass
            return

        # Mark registry entry as done so the polling loop stops.
        final_msg = (
            f"Done — {installed}/{total} mods installed into profile '{profile_dir.name}'."
            + (f" ({skipped} skipped)" if skipped else "")
        )
        _install_state["status"] = final_msg
        _install_state["done"] = True
        # Clean up registry after a short delay so a reconnecting panel can still read the final state.
        def _cleanup():
            _ACTIVE_INSTALLS.pop(_slug, None)
        try:
            self.after(5000, _cleanup)
        except Exception:
            _ACTIVE_INSTALLS.pop(_slug, None)

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

    # ------------------------------------------------------------------
    # Install overlay (blocks interaction while install is running)
    # ------------------------------------------------------------------

    def _show_install_overlay(self, mod_count: int, profile_name: str):
        """Show a semi-transparent overlay that blocks all interaction."""
        overlay = tk.Frame(self, bg="#1a1a1a")
        overlay.place(relx=0, rely=0, relwidth=1, relheight=1)
        overlay.lift()
        # Consume all clicks so nothing underneath is reachable
        overlay.bind("<Button-1>", lambda e: "break")
        overlay.bind("<ButtonRelease-1>", lambda e: "break")

        inner = tk.Frame(overlay, bg="#2b2b2b", width=500, bd=0, highlightthickness=0)
        inner.place(relx=0.5, rely=0.5, anchor="center", width=500)

        tk.Label(
            inner, text=f"Installing {mod_count} mods…" if mod_count else "Installing…",
            font=("Segoe UI", 16, "bold"), fg="#ffffff", bg="#2b2b2b",
            bd=0, highlightthickness=0,
        ).pack(pady=(20, 4))
        if profile_name:
            tk.Label(
                inner, text=f"Profile: {profile_name}",
                font=("Segoe UI", 12), fg="#aaaaaa", bg="#2b2b2b",
                bd=0, highlightthickness=0,
            ).pack(pady=(0, 4))

        # Dedicated overlay status label — mirrors self._status_var
        tk.Label(
            inner, textvariable=self._status_var,
            bg="#2b2b2b", fg="#aaaaaa",
            font=("Segoe UI", 11), anchor="w", bd=0, highlightthickness=0,
        ).pack(fill="x", padx=16, pady=(6, 2))

        # Dedicated overlay progress bar (install progress)
        overlay_bar = ctk.CTkProgressBar(
            inner, height=8, progress_color=ACCENT,
            fg_color=BG_PANEL, corner_radius=4,
        )
        overlay_bar.set(0)
        overlay_bar.pack(fill="x", padx=16, pady=(2, 4))

        # Download progress section (hidden until downloads start)
        dl_msg_lbl = tk.Label(
            inner, text="", bg="#2b2b2b", fg="#aaaaaa",
            font=("Segoe UI", 10), anchor="w", bd=0, highlightthickness=0,
        )
        dl_bar = ctk.CTkProgressBar(
            inner, height=6, progress_color=ACCENT,
            fg_color=BG_PANEL, corner_radius=4,
        )
        dl_bar.set(0)
        # Not packed yet — shown when download starts via _show_overlay_download

        # Button row — always packed last so dl_bar can be inserted before it
        btn_row = tk.Frame(inner, bg="#2b2b2b")
        btn_row.pack(pady=(8, 16))

        pause_btn = ctk.CTkButton(
            btn_row, text="Pause",
            height=scaled(28), width=scaled(110),
            fg_color="#7a5a00", hover_color="#a07800",
            text_color="#ffffff", font=font_sized("Segoe UI", 10),
            border_width=0,
            command=self._on_pause_install,
        )
        pause_btn.pack(side="left", padx=(0, 8))

        cancel_btn = ctk.CTkButton(
            btn_row, text="Cancel",
            height=scaled(28), width=scaled(110),
            fg_color="#7a1a1a", hover_color="#a02020",
            text_color="#ffffff", font=font_sized("Segoe UI", 10),
            border_width=0,
            command=self._on_cancel_install,
        )
        cancel_btn.pack(side="left")

        self._install_overlay = overlay
        self._install_overlay_bar = overlay_bar
        self._install_overlay_dl_msg = dl_msg_lbl
        self._install_overlay_dl_bar = dl_bar
        self._install_overlay_pause_btn = pause_btn
        self._install_overlay_btn_row = btn_row

    def _show_overlay_download(self, label: str):
        """Show the download progress section inside the install overlay."""
        lbl = getattr(self, "_install_overlay_dl_msg", None)
        bar = getattr(self, "_install_overlay_dl_bar", None)
        btn_row = getattr(self, "_install_overlay_btn_row", None)
        if lbl is None or bar is None:
            return
        lbl.configure(text=label)
        if not lbl.winfo_ismapped():
            pack_kw = {"before": btn_row} if btn_row is not None else {}
            lbl.pack(fill="x", padx=16, pady=(4, 0), **pack_kw)
            bar.pack(fill="x", padx=16, pady=(2, 8), **pack_kw)

    def _update_overlay_download(self, current: int, total: int, speed_mbs: float = 0.0):
        """Update the download progress bar and message in the overlay."""
        bar = getattr(self, "_install_overlay_dl_bar", None)
        lbl = getattr(self, "_install_overlay_dl_msg", None)
        if bar is None or lbl is None:
            return
        if total > 0:
            frac = min(current / total, 1.0)
            pct = int(frac * 100)
            _GB = 1024 * 1024 * 1024
            if total >= _GB:
                cur_u, tot_u, unit = current / _GB, total / _GB, "GB"
            else:
                cur_u, tot_u, unit = current / (1024 * 1024), total / (1024 * 1024), "MB"
            bar.set(frac)
            speed_str = f"  —  {speed_mbs:.1f} MB/s" if speed_mbs > 0.1 else ""
            lbl.configure(text=f"{cur_u:.2f} / {tot_u:.2f} {unit}  ({pct}%){speed_str}")

    def _hide_overlay_download(self):
        """Hide the download progress section in the overlay."""
        for w in (getattr(self, "_install_overlay_dl_msg", None),
                  getattr(self, "_install_overlay_dl_bar", None)):
            if w is not None:
                try:
                    w.pack_forget()
                except Exception:
                    pass

    def _dismiss_install_overlay(self):
        """Remove the install overlay."""
        overlay = getattr(self, "_install_overlay", None)
        if overlay is None:
            return
        try:
            overlay.destroy()
        except Exception:
            pass
        self._install_overlay = None
        self._install_overlay_bar = None
        self._install_overlay_dl_msg = None
        self._install_overlay_dl_bar = None
        self._install_overlay_pause_btn = None
        self._install_overlay_btn_row = None

    # ------------------------------------------------------------------
    # Manual (non-premium) install overlay + flow
    # ------------------------------------------------------------------

    def _show_manual_install_overlay(self, mod_count: int, profile_name: str):
        """Show a blocking overlay for sequential manual-download collection install."""
        overlay = tk.Frame(self, bg="#1a1a1a")
        overlay.place(relx=0, rely=0, relwidth=1, relheight=1)
        overlay.lift()
        overlay.bind("<Button-1>", lambda e: "break")
        overlay.bind("<ButtonRelease-1>", lambda e: "break")

        inner = tk.Frame(overlay, bg="#2b2b2b", bd=0, highlightthickness=0)
        inner.place(relx=0.5, rely=0.5, anchor="center", width=scaled(540))

        tk.Label(
            inner, text="Manual Download Required",
            font=("Segoe UI", 16, "bold"), fg="#ffffff", bg="#2b2b2b",
        ).pack(pady=(20, 2))
        tk.Label(
            inner, text=f"Non-premium users must download each mod manually.",
            font=("Segoe UI", 10), fg="#aaaaaa", bg="#2b2b2b",
        ).pack(pady=(0, 2))
        if profile_name:
            tk.Label(
                inner, text=f"Profile: {profile_name}",
                font=("Segoe UI", 11), fg="#aaaaaa", bg="#2b2b2b",
            ).pack(pady=(0, 6))

        # --- Mod info card ---
        card = tk.Frame(inner, bg="#333333", bd=0, highlightthickness=1, highlightbackground="#555555")
        card.pack(fill="x", padx=20, pady=(6, 4))

        self._manual_mod_name_lbl = tk.Label(
            card, text="", font=("Segoe UI", 13, "bold"), fg="#ffffff", bg="#333333",
            anchor="w", wraplength=scaled(480),
        )
        self._manual_mod_name_lbl.pack(fill="x", padx=12, pady=(10, 2))

        info_row = tk.Frame(card, bg="#333333")
        info_row.pack(fill="x", padx=12, pady=(0, 2))
        self._manual_mod_size_lbl = tk.Label(
            info_row, text="", font=("Segoe UI", 10), fg="#aaaaaa", bg="#333333", anchor="w",
        )
        self._manual_mod_size_lbl.pack(side="left")
        self._manual_mod_badge_lbl = tk.Label(
            info_row, text="", font=("Segoe UI", 9, "bold"), fg="#ffffff", bg="#2d7a2d",
            padx=6, pady=1,
        )
        self._manual_mod_badge_lbl.pack(side="left", padx=(8, 0))

        self._manual_mod_file_hint_lbl = tk.Label(
            card, text="", font=("Consolas", 9), fg="#777777", bg="#333333",
            anchor="w", wraplength=scaled(480),
        )
        self._manual_mod_file_hint_lbl.pack(fill="x", padx=12, pady=(0, 10))

        # --- Status ---
        self._manual_status_var = tk.StringVar(value="Preparing\u2026")
        tk.Label(
            inner, textvariable=self._manual_status_var,
            bg="#2b2b2b", fg="#aaaaaa", font=("Segoe UI", 10), anchor="w",
        ).pack(fill="x", padx=20, pady=(6, 2))

        # --- Buttons ---
        btn_row = tk.Frame(inner, bg="#2b2b2b")
        btn_row.pack(pady=(8, 4))

        self._manual_open_url_btn = ctk.CTkButton(
            btn_row, text="Open Download Page",
            height=32, width=200,  # unscaled — CTk applies set_widget_scaling internally
            fg_color=ACCENT, hover_color=ACCENT_HOV,
            text_color="#ffffff", font=font_sized("Segoe UI", 11),
            border_width=0,
            command=lambda: None,  # replaced per-mod
        )
        self._manual_open_url_btn.pack(side="left", padx=(0, 8))

        self._manual_open_next_btn = ctk.CTkButton(
            btn_row, text="Open next 5",
            height=32, width=110,  # unscaled — CTk applies set_widget_scaling internally
            fg_color="#1a5a8a", hover_color="#2070a8",
            text_color="#ffffff", font=font_sized("Segoe UI", 10),
            border_width=0,
            command=lambda: None,  # replaced per-mod
        )
        self._manual_open_next_btn.pack(side="left", padx=(0, 8))
        self._manual_open_next_btn.pack_forget()  # hidden until there are upcoming mods

        self._manual_select_btn = ctk.CTkButton(
            btn_row, text="Select File\u2026",
            height=32, width=120,  # unscaled — CTk applies set_widget_scaling internally
            fg_color="#444444", hover_color="#555555",
            text_color="#ffffff", font=font_sized("Segoe UI", 10),
            border_width=0,
            command=self._on_manual_select_file,
        )
        self._manual_select_btn.pack(side="left", padx=(0, 8))

        self._manual_skip_btn = ctk.CTkButton(
            btn_row, text="Skip",
            height=32, width=80,  # unscaled — CTk applies set_widget_scaling internally
            fg_color="#7a5a00", hover_color="#a07800",
            text_color="#ffffff", font=font_sized("Segoe UI", 10),
            border_width=0,
            command=self._on_manual_skip,
        )
        self._manual_skip_btn.pack(side="left", padx=(0, 8))
        self._manual_skip_btn.pack_forget()  # hidden by default

        # --- Auto-open checkbox ---
        self._manual_auto_open_var = tk.BooleanVar(value=False)
        ctk.CTkCheckBox(
            inner, text="Auto open next mod",
            variable=self._manual_auto_open_var,
            font=font_sized("Segoe UI", 10),
            text_color="#cccccc",
            fg_color=ACCENT, hover_color=ACCENT_HOV,
            border_color="#666666",
            checkmark_color="#ffffff",
            width=20, height=20,
        ).pack(anchor="w", padx=22, pady=(2, 4))

        # --- Bottom row: progress + cancel ---
        bottom = tk.Frame(inner, bg="#2b2b2b")
        bottom.pack(fill="x", padx=20, pady=(6, 16))

        self._manual_progress_lbl = tk.Label(
            bottom, text=f"0 of {mod_count} mods installed",
            font=("Segoe UI", 10), fg="#aaaaaa", bg="#2b2b2b", anchor="w",
        )
        self._manual_progress_lbl.pack(side="left")

        cancel_btn = ctk.CTkButton(
            bottom, text="Cancel",
            height=28, width=100,  # unscaled — CTk applies set_widget_scaling internally
            fg_color="#7a1a1a", hover_color="#a02020",
            text_color="#ffffff", font=font_sized("Segoe UI", 10),
            border_width=0,
            command=self._on_manual_cancel,
        )
        cancel_btn.pack(side="right")

        self._manual_overlay = overlay
        self._manual_cancel_event = threading.Event()
        self._manual_file_queue: _queue_mod.Queue = _queue_mod.Queue()

    def _update_manual_overlay(self, mod, idx: int, total: int, installed_so_far: int,
                               upcoming_mods: "list | None" = None):
        """Update the manual overlay to show the current mod being requested."""
        game_domain = getattr(self._game, "nexus_game_domain", None) or self._game_domain
        nexus_url = f"https://www.nexusmods.com/{game_domain}/mods/{mod.mod_id}?tab=files&file_id={mod.file_id}"

        self._manual_mod_name_lbl.configure(text=mod.mod_name or f"Mod {mod.mod_id}")
        size_str = _fmt_size(getattr(mod, "size_bytes", 0) or 0)
        self._manual_mod_size_lbl.configure(text=size_str)

        if getattr(mod, "optional", False):
            self._manual_mod_badge_lbl.configure(text="Optional", bg="#c37800")
            self._manual_skip_btn.pack(side="left", padx=(0, 8))
        else:
            self._manual_mod_badge_lbl.configure(text="Required", bg="#2d7a2d")
            self._manual_skip_btn.pack_forget()

        hint = mod.file_name or ""
        self._manual_mod_file_hint_lbl.configure(
            text=f"Expected file: {hint}" if hint else ""
        )
        self._manual_status_var.set(
            f"Mod {idx}/{total} — download this file, then it will be auto-detected\u2026"
        )
        self._manual_progress_lbl.configure(
            text=f"{installed_so_far} of {total} mods installed"
        )
        self._manual_open_url_btn.configure(command=lambda u=nexus_url: open_url(u, log_fn=self._log))

        # "Open next 5" button — current mod + up to 4 upcoming
        batch = [mod] + (upcoming_mods or [])[:4]
        # Always show "Open next 5" (current + upcoming), hide only if it would just duplicate "Open Download Page"
        if upcoming_mods:
            def _open_next(_mods=batch):
                _gd = getattr(self._game, "nexus_game_domain", None) or self._game_domain
                for _m in _mods:
                    _u = f"https://www.nexusmods.com/{_gd}/mods/{_m.mod_id}?tab=files&file_id={_m.file_id}"
                    open_url(_u, log_fn=self._log)
            count = len(batch)
            self._manual_open_next_btn.configure(
                text=f"Open next {count}",
                command=_open_next,
            )
            self._manual_open_next_btn.pack(side="left", padx=(0, 8))
        else:
            self._manual_open_next_btn.pack_forget()

    def _on_manual_skip(self):
        """Skip the current mod (only for optional mods)."""
        try:
            self._manual_file_queue.put_nowait(None)
        except Exception:
            pass

    def _on_manual_select_file(self):
        """Open a file picker as fallback for manual download detection."""
        from Utils.portal_filechooser import pick_file

        def _on_picked(path):
            if path is not None:
                try:
                    self._manual_file_queue.put_nowait(str(path))
                except Exception:
                    pass

        pick_file("Select downloaded mod archive", _on_picked)

    def _on_manual_cancel(self):
        """Cancel the manual install flow."""
        self._manual_cancel_event.set()

    def _dismiss_manual_overlay(self):
        """Remove the manual-download overlay."""
        overlay = getattr(self, "_manual_overlay", None)
        if overlay is None:
            return
        try:
            overlay.destroy()
        except Exception:
            pass
        self._manual_overlay = None

    # ------------------------------------------------------------------
    # _run_manual_install — sequential download+install for non-premium
    # ------------------------------------------------------------------

    def _run_manual_install(self, mods, download_link_path, profile_dir, old_profile,
                            downloader, app, total,
                            overwrite_existing: "bool | None" = None,
                            skipped_fids: "set[int] | None" = None,
                            skipped_mods: "list | None" = None):
        """Background thread: guide user through manual download of each mod, then install."""
        import time as _time_mod
        from Nexus.nexus_download import _find_cached_archive, _get_downloads_dir
        from gui.download_locations_overlay import load_extra_download_locations

        _slug = self._collection.slug or ""
        _install_state: dict = {"status": "", "installed_fids": set(), "done": False, "profile_dir": profile_dir}
        if _slug:
            _ACTIVE_INSTALLS[_slug] = _install_state

        def _set_status(msg: str):
            _install_state["status"] = msg
            try:
                self.after(0, lambda m=msg: self._manual_status_var.set(m))
            except Exception:
                pass

        self._game.set_active_profile_dir(profile_dir)
        modlist_path = profile_dir / "modlist.txt"
        staging_path = self._game.get_effective_mod_staging_path()
        installed = 0
        skipped = 0

        # ------------------------------------------------------------------
        # Step 1: Parse collection.json (same as _run_install)
        # ------------------------------------------------------------------
        collection_schema: dict = {}
        cached_schema = getattr(self, "_collection_schema_cache", None)
        if cached_schema:
            collection_schema = cached_schema
            self._log("Manual install: reusing cached collection.json")
        elif download_link_path:
            _set_status("Downloading collection manifest\u2026")
            try:
                collection_schema = self._api.get_collection_archive_json(download_link_path)
            except Exception as exc:
                self._log(f"Manual install: could not download collection.json: {exc}")

        if collection_schema:
            try:
                import json as _json
                (profile_dir / "collection.json").write_text(
                    _json.dumps(collection_schema, indent=2), encoding="utf-8",
                )
            except Exception:
                pass

        schema_mods: list[dict] = collection_schema.get("mods", [])
        mod_rules: list[dict] = collection_schema.get("modRules", [])
        schema_file_id_to_pos = _topo_sort_collection(schema_mods, mod_rules)
        schema_pos_to_name: dict[int, str] = {}
        schema_file_id_to_logical: dict[int, str] = {}
        schema_file_id_to_mod_id: dict[int, int] = {}
        fomod_by_file_id: dict[int, dict] = {}

        _raw_logical: dict[int, str] = {}
        _raw_name: dict[int, str] = {}
        for sm in schema_mods:
            src = sm.get("source") or {}
            fid = src.get("fileId")
            if fid is not None:
                fid = int(fid)
                _raw_logical[fid] = src.get("logicalFilename") or ""
                _raw_name[fid] = sm.get("name") or ""
        _logical_counts: dict[str, int] = {}
        for raw in _raw_logical.values():
            if raw:
                _logical_counts[raw] = _logical_counts.get(raw, 0) + 1

        for pos, sm in enumerate(schema_mods):
            src = sm.get("source") or {}
            fid = src.get("fileId")
            if fid is not None:
                fid = int(fid)
                topo_pos = schema_file_id_to_pos.get(fid, pos)
                schema_pos_to_name[topo_pos] = sm.get("name") or ""
                raw_logical = _raw_logical.get(fid, "")
                schema_name = _raw_name.get(fid, "")
                if raw_logical and _logical_counts.get(raw_logical, 0) > 1:
                    logical = schema_name or raw_logical
                else:
                    logical = raw_logical or schema_name
                schema_file_id_to_logical[fid] = logical
                mid = src.get("modId")
                if mid:
                    schema_file_id_to_mod_id[fid] = int(mid)
                choices = sm.get("choices") or {}
                if choices.get("type") == "fomod":
                    fomod_by_file_id[fid] = _fomod_choices_from_collection(choices)
                elif choices.get("type") == "fomod_selections":
                    fomod_by_file_id[fid] = choices["selections"]

        def _sort_key(m):
            return schema_file_id_to_pos.get(m.file_id, len(schema_mods))

        ordered_mods = sorted(mods, key=_sort_key)

        # ------------------------------------------------------------------
        # Step 2: Classify already-installed mods (same as _run_install)
        # ------------------------------------------------------------------
        already_installed_by_fid: dict[int, str] = {}
        staging_lower_map: dict[str, str] = {}
        _profile_mod_names: set[str] = set()
        if modlist_path.is_file():
            try:
                for entry in read_modlist(modlist_path):
                    _profile_mod_names.add(entry.name.lower())
            except Exception:
                pass

        import configparser as _cp
        if staging_path.exists():
            for mod_dir in staging_path.iterdir():
                if not mod_dir.is_dir():
                    continue
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

        # Remove staging folders for unticked optional mods
        if skipped_fids and skipped_mods:
            import shutil as _shutil_skip
            _removed: list[str] = []
            for mod in skipped_mods:
                if not mod.file_id or mod.file_id not in skipped_fids:
                    continue
                folder_name = already_installed_by_fid.get(mod.file_id, "")
                if not folder_name:
                    logical = schema_file_id_to_logical.get(mod.file_id, "") or ""
                    schema_name = schema_pos_to_name.get(
                        schema_file_id_to_pos.get(mod.file_id, -1), "") or ""
                    candidates: list[str] = []
                    name_sources = (logical, schema_name) if (logical or schema_name) else (mod.mod_name or "",)
                    for raw in name_sources:
                        if raw:
                            for s in _suggest_mod_names(raw):
                                if s and s not in candidates:
                                    candidates.append(s)
                    for candidate in candidates:
                        if candidate.lower() in staging_lower_map:
                            folder_name = staging_lower_map[candidate.lower()]
                            break
                if folder_name:
                    skip_dir = staging_path / folder_name
                    if skip_dir.is_dir():
                        self._log(f"Manual install: removing unticked '{folder_name}'")
                        try:
                            _shutil_skip.rmtree(skip_dir)
                            _removed.append(folder_name)
                        except Exception:
                            pass
            if _removed and modlist_path.is_file():
                try:
                    _rem_set = set(_removed)
                    entries = read_modlist(modlist_path)
                    entries = [e for e in entries if e.name not in _rem_set]
                    write_modlist(modlist_path, entries)
                except Exception:
                    pass

        install_order: list[tuple[int, str]] = []
        to_download: list = []
        for mod in ordered_mods:
            if not mod.file_id:
                skipped += 1
                continue
            existing_folder = ""
            if mod.file_id in already_installed_by_fid:
                existing_folder = already_installed_by_fid[mod.file_id]
            else:
                logical = schema_file_id_to_logical.get(mod.file_id, "") or ""
                schema_name = schema_pos_to_name.get(schema_file_id_to_pos.get(mod.file_id, -1), "") or ""
                candidates = []
                name_sources = (logical, schema_name) if (logical or schema_name) else (mod.mod_name or "",)
                for raw in name_sources:
                    if raw:
                        for s in _suggest_mod_names(raw):
                            if s and s not in candidates:
                                candidates.append(s)
                for candidate in candidates:
                    if candidate.lower() in staging_lower_map:
                        existing_folder = staging_lower_map[candidate.lower()]
                        break
            if existing_folder:
                self._log(f"Manual install: '{mod.mod_name}' already installed as '{existing_folder}' \u2014 skipping")
                install_order.append((_sort_key(mod), existing_folder))
                installed += 1
            else:
                to_download.append(mod)

        # Sort by size per collection settings (mirrors _run_install)
        from Utils.ui_config import load_collection_settings as _load_col_cfg
        _col_cfg = _load_col_cfg()
        to_download.sort(
            key=lambda m: getattr(m, "size_bytes", 0) or 0,
            reverse=(_col_cfg["download_order"] == "largest"),
        )

        # ------------------------------------------------------------------
        # Step 3: Sequential manual download + install
        # ------------------------------------------------------------------
        def _get_scan_dirs() -> list[Path]:
            dirs: list[Path] = [_get_downloads_dir()]
            seen = {dirs[0].resolve()}
            for p in load_extra_download_locations():
                path = Path(p).expanduser().resolve()
                if path.is_dir() and path not in seen:
                    dirs.append(path)
                    seen.add(path)
            return dirs

        def _wait_for_file(mod) -> "Path | None":
            """Poll downloads folders until the mod archive appears, or user skips/selects."""
            scan_dirs = _get_scan_dirs()
            while not self._manual_cancel_event.is_set():
                # Check user actions (select file / skip)
                try:
                    item = self._manual_file_queue.get_nowait()
                    if item is None:
                        return None  # skip
                    p = Path(item)
                    if p.is_file():
                        return p
                except _queue_mod.Empty:
                    pass
                # Poll downloads folders
                for folder in scan_dirs:
                    if not folder.is_dir():
                        continue
                    found, is_complete = _find_cached_archive(
                        folder,
                        mod.file_name or mod.mod_name or "",
                        getattr(mod, "size_bytes", 0) or 0,
                        mod.mod_id,
                        mod.file_id,
                    )
                    if found and is_complete:
                        return found
                _time_mod.sleep(2.0)
            return None  # cancelled

        dl_total = len(to_download)
        for idx_0, mod in enumerate(to_download):
            if self._manual_cancel_event.is_set():
                break

            idx = idx_0 + 1
            # Update overlay on main thread and wait for it to complete
            _ready = threading.Event()
            def _do_update(_m=mod, _i=idx, _t=dl_total, _inst=installed, _up=to_download[idx_0+1:]):
                try:
                    self._update_manual_overlay(_m, _i, _t, _inst, upcoming_mods=_up)
                except Exception:
                    pass
                finally:
                    _ready.set()
            try:
                self.after(0, _do_update)
            except Exception:
                _ready.set()
            _ready.wait(timeout=5)

            # Wait for the file to appear
            archive_path = _wait_for_file(mod)

            if self._manual_cancel_event.is_set():
                break
            if archive_path is None:
                self._log(f"Manual install: skipped '{mod.mod_name}'")
                skipped += 1
                continue

            _set_status(f"Installing {mod.mod_name}\u2026")

            # Build prebuilt metadata
            try:
                _effective_mod_id = schema_file_id_to_mod_id.get(mod.file_id, 0) or mod.mod_id
                _pmeta = build_meta_from_download(
                    game_domain=self._game_domain,
                    mod_id=_effective_mod_id,
                    file_id=mod.file_id,
                    archive_name=mod.file_name or "",
                )
                _pmeta.nexus_name = mod.mod_name or ""
                _pmeta.author = mod.mod_author or ""
                _pmeta.version = mod.version or ""
            except Exception:
                _pmeta = None

            _logical = schema_file_id_to_logical.get(mod.file_id, "") or ""
            _schema_name = schema_pos_to_name.get(
                schema_file_id_to_pos.get(mod.file_id, -1), "") or ""
            _preferred = _logical or _schema_name or mod.mod_name or ""

            try:
                folder_name = install_mod_from_archive(
                    str(archive_path), self, self._log, self._game,
                    fomod_auto_selections=fomod_by_file_id.get(mod.file_id),
                    prebuilt_meta=_pmeta,
                    profile_dir=profile_dir,
                    headless=True,
                    preferred_name=_preferred,
                    skip_index_update=True,
                    overwrite_existing=overwrite_existing,
                )
            except Exception as exc:
                self._log(f"Manual install: failed to install '{mod.mod_name}': {exc}")
                folder_name = None

            if folder_name:
                installed += 1
                install_order.append((_sort_key(mod), folder_name))
                _install_state["installed_fids"].add(mod.file_id)
                try:
                    self.after(0, lambda fid=mod.file_id: self._mark_row_installed(fid))
                except Exception:
                    pass
                # Delete the archive after a successful install
                try:
                    archive_path.unlink(missing_ok=True)
                    self._log(f"Manual install: deleted archive '{archive_path.name}'")
                except Exception as _del_exc:
                    self._log(f"Manual install: could not delete archive '{archive_path.name}': {_del_exc}")
                # Auto-open next mod's download page if checkbox is ticked
                _next_mods = to_download[idx_0 + 1:]
                if _next_mods and getattr(self, "_manual_auto_open_var", None) and self._manual_auto_open_var.get():
                    _next_mod = _next_mods[0]
                    _gd = getattr(self._game, "nexus_game_domain", None) or self._game_domain
                    _auto_url = f"https://www.nexusmods.com/{_gd}/mods/{_next_mod.mod_id}?tab=files&file_id={_next_mod.file_id}"
                    try:
                        open_url(_auto_url, log_fn=self._log)
                    except Exception:
                        pass
            else:
                skipped += 1

        # ------------------------------------------------------------------
        # Step 4: Bundled assets from collection archive (same as _run_install)
        # ------------------------------------------------------------------
        bundle_schema_mods = [
            m for m in schema_mods
            if (m.get("source") or {}).get("type", "").lower() == "bundle"
        ]
        if bundle_schema_mods and download_link_path:
            import tempfile as _tf
            _set_status(f"Installing {len(bundle_schema_mods)} bundled mod(s)\u2026")
            bundle_extract_dir = _tf.mkdtemp(prefix="amethyst_bundle_")
            try:
                cj_full = self._api.get_collection_archive_full(
                    download_link_path, bundle_extract_dir,
                )
                if cj_full:
                    import shutil as _shutil2
                    import configparser as _cpi
                    for bm in bundle_schema_mods:
                        bm_name = bm.get("name") or ""
                        src = bm.get("source") or {}
                        file_expr = src.get("fileExpression") or bm_name
                        bundle_subdir = Path(bundle_extract_dir) / "bundled" / file_expr
                        if not bundle_subdir.is_dir():
                            bundle_subdir = Path(bundle_extract_dir) / "bundled" / bm_name
                        if not bundle_subdir.is_dir():
                            skipped += 1
                            continue
                        mod_name_clean = re.sub(r"[^\w\s\-]", "", bm_name).strip().replace(" ", "_") or file_expr
                        if mod_name_clean.lower() in {k.lower() for k in staging_lower_map}:
                            existing = staging_lower_map.get(mod_name_clean.lower(), mod_name_clean)
                            install_order.append((-1, existing))
                            installed += 1
                            continue
                        dest = staging_path / mod_name_clean
                        if dest.exists():
                            _shutil2.rmtree(dest)
                        _shutil2.copytree(str(bundle_subdir), str(dest))
                        meta = dest / "meta.ini"
                        cp = _cpi.ConfigParser()
                        cp["General"] = {"modname": bm_name, "installationfile": file_expr}
                        with open(meta, "w", encoding="utf-8") as mf:
                            cp.write(mf)
                        install_order.append((-1, mod_name_clean))
                        installed += 1
            except Exception as exc:
                self._log(f"Manual install: bundled assets error: {exc}")
            finally:
                import shutil as _shutil
                _shutil.rmtree(bundle_extract_dir, ignore_errors=True)

        # ------------------------------------------------------------------
        # Step 5: Rebuild mod index
        # ------------------------------------------------------------------
        if installed > 0:
            try:
                _idx_path = profile_dir / "modindex.bin"
                rebuild_mod_index(
                    _idx_path,
                    self._game.get_effective_mod_staging_path(),
                    strip_prefixes=set(getattr(self._game, "strip_prefixes", None) or []),
                    allowed_extensions=set(getattr(self._game, "install_extensions", None) or []),
                    root_deploy_folders=set(getattr(self._game, "root_deploy_folders", None) or []),
                    normalize_folder_case=getattr(self._game, "normalize_folder_case", True),
                )
            except Exception:
                pass

        # Build install_order for downloaded mods
        for mod in to_download:
            if mod.file_id in _install_state["installed_fids"]:
                # already appended in the loop above
                pass

        # ------------------------------------------------------------------
        # Step 6: Write modlist.txt in collection-defined order
        # ------------------------------------------------------------------
        if overwrite_existing is None:
            install_order.sort(key=lambda x: x[0])
            modlist_entries = [
                ModEntry(name=folder, enabled=True, locked=False)
                for _, folder in install_order
            ]
            if modlist_entries:
                try:
                    _bundle_map: dict[str, list[ModEntry]] = {}
                    _non_bundle: list[ModEntry] = []
                    for me in modlist_entries:
                        if "__" in me.name:
                            bname = me.name.split("__", 1)[0]
                            _bundle_map.setdefault(bname, []).append(me)
                        else:
                            _non_bundle.append(me)
                    final_entries: list[ModEntry] = list(_non_bundle)
                    for bname, variants in _bundle_map.items():
                        sep_name = f"{bname}_separator"
                        final_entries.append(
                            ModEntry(name=sep_name, enabled=True, locked=True, is_separator=True))
                        for v in variants:
                            v.locked = False
                            v.enabled = True
                            final_entries.append(v)
                    write_modlist(modlist_path, final_entries)
                    if _bundle_map:
                        from Utils.profile_state import read_separator_locks, write_separator_locks
                        _locks = read_separator_locks(profile_dir)
                        for bname in _bundle_map:
                            _locks[f"{bname}_separator"] = True
                        write_separator_locks(profile_dir, _locks)
                except Exception as exc:
                    self._log(f"Manual install: failed to write modlist.txt: {exc}")

        # ------------------------------------------------------------------
        # Step 7: Write plugins.txt / loadorder.txt from collection.json
        # ------------------------------------------------------------------
        schema_plugins: list[dict] = collection_schema.get("plugins", [])
        if schema_plugins and overwrite_existing is None:
            try:
                author_entries = [
                    PluginEntry(name=p.get("name", ""), enabled=p.get("enabled", True))
                    for p in schema_plugins if p.get("name", "")
                ]
                author_lower = {e.name.lower() for e in author_entries}
                vanilla_map = _vanilla_plugins_for_game(self._game)
                plugins_include_vanilla = getattr(self._game, "plugins_include_vanilla", False)
                vanilla_lower: set[str] = set() if plugins_include_vanilla else set(vanilla_map.keys())

                loot_vanilla_prefix: list[PluginEntry] = []
                loot_enabled = getattr(self._game, "loot_sort_enabled", False)
                if loot_enabled and _loot_available():
                    try:
                        _ext_order = {".esm": 0, ".esp": 1, ".esl": 2}
                        vanilla_prepend = [
                            PluginEntry(name=orig, enabled=True)
                            for low, orig in sorted(
                                vanilla_map.items(),
                                key=lambda kv: (_ext_order.get(Path(kv[0]).suffix, 9), kv[0]),
                            )
                            if low not in author_lower
                        ]
                        all_entries = vanilla_prepend + author_entries
                        name_to_enabled = {e.name: e.enabled for e in all_entries}
                        loot_result = _loot_sort(
                            plugin_names=[e.name for e in all_entries],
                            enabled_set={e.name for e in all_entries if e.enabled},
                            game_name=self._game.name,
                            game_path=self._game.get_game_path(),
                            staging_root=self._game.get_effective_mod_staging_path(),
                            log_fn=self._log,
                            game_type_attr=getattr(self._game, "loot_game_type", ""),
                            game_id=getattr(self._game, "game_id", ""),
                            masterlist_url=getattr(self._game, "loot_masterlist_url", ""),
                            game_data_dir=(
                                self._game.get_vanilla_plugins_path()
                                if hasattr(self._game, "get_vanilla_plugins_path") else None
                            ),
                        )
                        loot_vanilla_prefix = [
                            PluginEntry(name=n, enabled=name_to_enabled.get(n, True))
                            for n in loot_result.sorted_names
                            if n.lower() not in author_lower
                        ]
                    except Exception as exc:
                        self._log(f"Manual install: LOOT sort failed: {exc}")

                final_entries = loot_vanilla_prefix + author_entries
                star_prefix = getattr(self._game, "plugins_star_prefix", False)
                write_plugins(
                    profile_dir / "plugins.txt",
                    [e for e in final_entries if e.name.lower() not in vanilla_lower],
                    star_prefix=star_prefix,
                )
                write_loadorder(profile_dir / "loadorder.txt", final_entries)
            except Exception as exc:
                self._log(f"Manual install: failed to write plugins.txt: {exc}")

        # ------------------------------------------------------------------
        # Step 8: Final reconciliation
        # ------------------------------------------------------------------
        if install_order and modlist_path.is_file():
            try:
                _folder_to_key = {folder: key for key, folder in install_order}
                _existing = read_modlist(modlist_path)
                _known = [e for e in _existing if e.name in _folder_to_key]
                _unknown = [e for e in _existing if e.name not in _folder_to_key]
                for e in _known:
                    e.enabled = True
                for e in _unknown:
                    if not e.is_separator:
                        e.enabled = True
                _known.sort(key=lambda e: _folder_to_key[e.name])
                write_modlist(modlist_path, _known + _unknown)
            except Exception:
                pass

        self._game.set_active_profile_dir(old_profile)

        # Handle cancel
        if self._manual_cancel_event.is_set():
            _install_state["done"] = True
            _ACTIVE_INSTALLS.pop(_slug, None)
            try:
                self.after(0, lambda: (self._dismiss_manual_overlay(), self._status_var.set("Install cancelled.")))
            except Exception:
                pass
            return

        _install_state["done"] = True
        _ACTIVE_INSTALLS.pop(_slug, None)

        try:
            self.after(0, lambda: (
                self._dismiss_manual_overlay(),
                self._on_install_done(installed, skipped, total, str(profile_dir.name)),
            ))
        except Exception:
            pass

    def _on_install_done(self, installed: int, skipped: int, total: int, profile_name: str):
        self._dismiss_install_overlay()
        self._status_var.set(
            f"Done — {installed}/{total} mods installed into profile '{profile_name}'."
            + (f" ({skipped} skipped)" if skipped else "")
        )
        self._log(
            f"Collection install complete: {installed} installed, {skipped} skipped."
        )
        try:
            self._install_progress_bar.pack_forget()
        except Exception:
            pass
        self._refresh_profile_menu()
        # Auto-switch to the installed collection's profile
        self._switch_to_profile(profile_name)
        self._update_reset_btn_visibility()
        self._update_open_missing_btn_visibility()
        self._update_install_btn_state()

    def _on_pause_install(self):
        """Called when the Pause button in the overlay is clicked."""
        slug = self._collection.slug or ""
        state = _ACTIVE_INSTALLS.get(slug)
        if state is None:
            return
        pause_evt = state.get("pause")
        if pause_evt is not None:
            pause_evt.set()
        self._status_var.set("Pausing… waiting for current downloads/installs to finish.")
        # Disable the button so it can't be clicked twice
        btn = getattr(self, "_install_overlay_pause_btn", None)
        if btn is not None:
            try:
                btn.configure(state="disabled", text="Pausing…")
            except Exception:
                pass

    def _on_cancel_install(self):
        """Ask for confirmation then cancel the install, wipe the download cache and delete the profile."""
        slug = self._collection.slug or ""
        state = _ACTIVE_INSTALLS.get(slug)
        if state is None:
            return

        # Get the app root window for the alert parent
        app_root = getattr(self, "_app_root", None)
        alert_parent = app_root if app_root is not None else self

        alert = CTkAlert(
            state="warning",
            title="Cancel Install",
            body_text=(
                "Are you sure you want to cancel?\n\n"
                "This will stop the install, clear the download cache, "
                "and delete the collection profile."
            ),
            btn1="Cancel Install",
            btn2="Keep Going",
            parent=alert_parent,
        )
        result = alert.get()
        if result != "Cancel Install":
            return

        # Signal the background thread to stop
        cancel_evt = state.get("cancel")
        if cancel_evt is not None:
            cancel_evt.set()
        pause_evt = state.get("pause")
        if pause_evt is not None:
            pause_evt.set()

        self._status_var.set("Cancelling… waiting for current operations to finish.")
        btn_row = getattr(self, "_install_overlay_btn_row", None)
        if btn_row is not None:
            for child in btn_row.winfo_children():
                try:
                    child.configure(state="disabled")
                except Exception:
                    pass
        # Cleanup is triggered by _run_install itself once it detects _col_cancel
        # and winds down — it calls self.after(0, _do_cancel_cleanup(profile_dir))

    def _do_cancel_cleanup(self, profile_dir=None):
        """Restore game, delete profile dir, wipe download cache, switch to default profile."""
        import shutil as _shutil_cancel
        slug = self._collection.slug or ""
        _ACTIVE_INSTALLS.pop(slug, None)
        _PAUSED_INSTALLS.pop(slug, None)
        if profile_dir is None:
            profile_dir = self._get_profile_dir()

        game = self._game

        # Restore any deployed mod files so we don't orphan files in the game folder
        if profile_dir is not None and profile_dir.is_dir() and game is not None and game.is_configured():
            game.set_active_profile_dir(profile_dir)
            try:
                if hasattr(game, "restore"):
                    game.restore()
            except Exception as exc:
                self._log(f"Cancel: restore failed: {exc}")
            try:
                from Utils.deploy import restore_root_folder
                root_folder_dir = game.get_effective_root_folder_path()
                game_root = game.get_game_path()
                if root_folder_dir.is_dir() and game_root:
                    restore_root_folder(root_folder_dir, game_root)
            except Exception as exc:
                self._log(f"Cancel: restore_root_folder failed: {exc}")
            game.set_active_profile_dir(None)

        # Delete the collection profile directory
        if profile_dir is not None and profile_dir.is_dir():
            try:
                _shutil_cancel.rmtree(str(profile_dir))
                self._log(f"Cancel: deleted profile dir {profile_dir}")
            except Exception as exc:
                self._log(f"Cancel: failed to delete profile dir: {exc}")

        # Clear the download cache
        try:
            cache_dir = get_download_cache_dir()
            if cache_dir and cache_dir.is_dir():
                for item in cache_dir.iterdir():
                    try:
                        if item.is_file() or item.is_symlink():
                            item.unlink()
                        elif item.is_dir():
                            _shutil_cancel.rmtree(str(item), ignore_errors=True)
                    except Exception:
                        pass
                self._log("Cancel: cleared download cache")
        except Exception as exc:
            self._log(f"Cancel: failed to clear download cache: {exc}")

        # Switch topbar back to default profile
        try:
            topbar = getattr(self._app_root, "_topbar", None)
            if topbar is not None and game is not None:
                profiles = _profiles_for_game(game.name)
                topbar._profile_menu.configure(values=profiles)
                topbar._profile_var.set(profiles[0])
                topbar._update_profile_menu_color()
                topbar._reload_mod_panel()
        except Exception as exc:
            self._log(f"Cancel: failed to switch profile: {exc}")

        self._dismiss_install_overlay()
        self._status_var.set("Install cancelled.")
        try:
            self._install_progress_bar.pack_forget()
        except Exception:
            pass
        self._update_install_btn_state()
        self._update_reset_btn_visibility()

    def _on_install_paused(self, installed: int, profile_name: str):
        """Called from background thread (via after()) when the install has fully paused."""
        self._dismiss_install_overlay()
        self._status_var.set(
            f"Paused — {installed} mod(s) installed so far. Click Resume to continue."
        )
        try:
            self._install_progress_bar.pack_forget()
        except Exception:
            pass
        self._update_install_btn_state()

    def _update_install_btn_state(self):
        """Show Install button as orange Resume if a paused install exists, else green Install."""
        btn = getattr(self, "_install_btn", None)
        if btn is None:
            return
        slug = self._collection.slug or ""
        if slug and slug in _PAUSED_INSTALLS:
            try:
                btn.configure(
                    text="Resume Install",
                    fg_color="#b35a00", hover_color="#d97000",
                )
            except Exception:
                pass
        else:
            try:
                btn.configure(
                    text="Install Collection",
                    fg_color="#2d7a2d", hover_color="#3a9e3a",
                )
            except Exception:
                pass

    def _switch_to_profile(self, profile_name: str):
        """Switch the app to the given profile."""
        try:
            topbar = getattr(self._app_root, "_topbar", None)
            if topbar is None:
                return
            topbar._profile_var.set(profile_name)
            topbar._on_profile_change(profile_name)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Install-progress reconnect (survives panel close/reopen)
    # ------------------------------------------------------------------
    def _maybe_reconnect_install(self) -> None:
        """If an install is already running for this collection, start polling it."""
        slug = self._collection.slug or ""
        self._update_install_btn_state()
        if slug and slug in _ACTIVE_INSTALLS:
            state = _ACTIVE_INSTALLS[slug]
            if not state.get("done") and not getattr(self, "_install_overlay", None):
                self._show_install_overlay(0, "")
            self._poll_install_progress()

    def _poll_install_progress(self) -> None:
        """Poll _ACTIVE_INSTALLS for this collection and sync status + row colours."""
        slug = self._collection.slug or ""
        state = _ACTIVE_INSTALLS.get(slug)
        if state is None:
            self._install_poll_id = None
            return

        # Sync status label
        status = state.get("status", "")
        if status:
            try:
                self._status_var.set(status)
            except Exception:
                pass

        # Green any rows installed since panel was last open
        for fid in list(state.get("installed_fids", set())):
            try:
                self._mark_row_installed(fid)
            except Exception:
                pass

        if state.get("done"):
            self._dismiss_install_overlay()
            # Final refresh so buttons update
            try:
                self._update_reset_btn_visibility()
                self._update_open_missing_btn_visibility()
            except Exception:
                pass
            self._install_poll_id = None
            return

        # Schedule next poll in 500 ms
        try:
            self._install_poll_id = self.after(500, self._poll_install_progress)
        except Exception:
            self._install_poll_id = None

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

    def _update_offsite_panel(self):
        """Rebuild the off-site mods panel below the treeview."""
        if self._offsite_frame is None:
            return
        try:
            if not self._offsite_frame.winfo_exists():
                return
        except Exception:
            return
        # Clear existing children
        for w in self._offsite_frame.winfo_children():
            w.destroy()

        if not self._offsite_mods:
            self._offsite_frame.pack_forget()
            return

        # Header row
        hdr = tk.Frame(self._offsite_frame, bg=BG_HEADER, pady=4)
        hdr.pack(fill="x")
        tk.Label(
            hdr,
            text=f"Off-site mods ({len(self._offsite_mods)}) — must be downloaded manually:",
            bg=BG_HEADER, fg=TEXT_DIM, font=font_sized_px("Segoe UI", 9), anchor="w",
        ).pack(side="left", padx=10)

        # Scrollable rows
        ROW_H = scaled(28)
        MAX_VISIBLE = 4
        visible = min(len(self._offsite_mods), MAX_VISIBLE)
        rows_frame = tk.Frame(self._offsite_frame, bg=BG_PANEL, height=visible * ROW_H)
        rows_frame.pack(fill="x")
        rows_frame.pack_propagate(False)

        canvas = tk.Canvas(rows_frame, bg=BG_PANEL, highlightthickness=0, bd=0)
        sb = tk.Scrollbar(rows_frame, orient="vertical", bg=BG_SEP, troughcolor=BG_DEEP,
                          activebackground=ACCENT, highlightthickness=0, bd=0, width=scaled(10))
        canvas.configure(yscrollcommand=sb.set)
        sb.config(command=canvas.yview)

        if len(self._offsite_mods) > MAX_VISIBLE:
            sb.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)

        inner = tk.Frame(canvas, bg=BG_PANEL)
        canvas_window = canvas.create_window((0, 0), window=inner, anchor="nw")

        def _on_inner_configure(_e=None):
            canvas.configure(scrollregion=canvas.bbox("all"))

        def _on_canvas_configure(e):
            canvas.itemconfig(canvas_window, width=e.width)

        inner.bind("<Configure>", _on_inner_configure)
        canvas.bind("<Configure>", _on_canvas_configure)

        for i, (name, url) in enumerate(self._offsite_mods):
            row_bg = BG_ROW if i % 2 else BG_PANEL
            row = tk.Frame(inner, bg=row_bg, height=ROW_H)
            row.pack(fill="x")
            row.pack_propagate(False)

            tk.Label(
                row, text=name or url, bg=row_bg, fg=TEXT_MAIN,
                font=font_sized_px("Segoe UI", 9), anchor="w",
            ).pack(side="left", padx=(10, 4), fill="x", expand=True)

            _url = url  # capture for lambda
            ctk.CTkButton(
                row, text="Open", width=scaled(55), height=scaled(22),
                fg_color=ACCENT, hover_color=ACCENT_HOV,
                text_color="#ffffff", font=font_sized("Segoe UI", 9),
                border_width=0,
                command=lambda u=_url: open_url(u),
            ).pack(side="right", padx=6, pady=3)

        # Pack the offsite frame below the priority note
        self._offsite_frame.pack(fill="x", side="top", after=self._priority_note)

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

    def _on_open_on_nexus(self):
        """Open this collection's Nexus page in the browser."""
        slug = self._collection.slug or ""
        if not slug:
            return
        url = f"https://www.nexusmods.com/games/{self._game_domain}/collections/{slug}"
        if self._revision_number:
            url += f"/revisions/{self._revision_number}"
        webbrowser.open(url)

    def _on_open_missing_on_nexus(self):
        """Open Nexus pages for all mods in the collection that are not installed."""
        missing = self._get_missing_mod_ids()
        if not missing:
            return
        _OPEN_LIMIT = 10
        for mod_id in sorted(missing)[:_OPEN_LIMIT]:
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
        """Re-apply collection.json load order to modlist.txt + plugins.txt."""
        import configparser
        try:
            # Use cached manifest if available; download only as fallback
            cj = getattr(self, "_collection_schema_cache", None) or {}
            if not cj:
                manifest_path = profile_dir / "collection.json"
                if manifest_path.is_file():
                    try:
                        import json as _json
                        cj = _json.loads(manifest_path.read_text(encoding="utf-8"))
                        self._log("Reset load order: using cached collection.json from profile")
                    except Exception:
                        pass
            if not cj:
                self.after(0, lambda: self._status_var.set("Downloading collection manifest…"))
                cj = self._api.get_collection_archive_json(self._download_link_path)
                self._collection_schema_cache = cj

            # Save manifest to profile dir for inspection
            if cj:
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

            # Build name-based fallback: logical_name → file_id (for mods missing meta.ini)
            _name_to_fid: dict[str, int] = {}
            for _sm in cj.get("mods", []):
                _src = _sm.get("source") or {}
                _sf = _src.get("fileId")
                if _sf is not None:
                    for _n in ((_src.get("logicalFilename") or "").strip(),
                               (_sm.get("name") or "").strip()):
                        if _n and _n.lower() not in _name_to_fid:
                            _name_to_fid[_n.lower()] = int(_sf)

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
                # Fallback: match folder name against schema logical/mod names
                if fid is None:
                    fid = _name_to_fid.get(folder.name.lower())
                if fid is not None and fid in fid_to_pos:
                    ordered.append((fid_to_pos[fid], folder.name))
                else:
                    unordered.append(folder.name)

            ordered.sort(key=lambda x: x[0])  # position 0 = highest priority → top
            # Unmatched mods (not in collection schema) go at the bottom
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

            # Re-write plugins.txt and loadorder.txt from collection.json
            schema_plugins: list = cj.get("plugins", [])
            if schema_plugins:
                try:
                    lines = []
                    loadorder_lines = []
                    for plugin in schema_plugins:
                        name = plugin.get("name", "")
                        enabled = plugin.get("enabled", True)
                        lines.append(("*" if enabled else "") + name)
                        loadorder_lines.append(name)
                    plugins_path = profile_dir / "plugins.txt"
                    plugins_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
                    self._log(f"Reset load order: wrote plugins.txt with {len(lines)} plugins")
                    # Preserve vanilla plugins already in loadorder.txt (they must stay at top)
                    loadorder_path = profile_dir / "loadorder.txt"
                    collection_lower = {n.lower() for n in loadorder_lines}
                    vanilla_prefix: list[str] = []
                    if loadorder_path.exists():
                        for lo_line in loadorder_path.read_text(encoding="utf-8").splitlines():
                            lo_line = lo_line.strip()
                            if lo_line and lo_line.lower() not in collection_lower:
                                vanilla_prefix.append(lo_line)
                    final_loadorder = vanilla_prefix + loadorder_lines
                    loadorder_path.write_text("\n".join(final_loadorder) + "\n", encoding="utf-8")
                    self._log(f"Reset load order: wrote loadorder.txt with {len(final_loadorder)} plugins ({len(vanilla_prefix)} vanilla)")
                except Exception as exc:
                    self._log(f"Reset load order: failed to write plugins.txt: {exc}")

            msg = (
                f"Load order reset — {len(ordered)} mods ordered"
                + (f", {len(unordered)} unmatched (placed at top)." if unordered else ".")
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
        on_open_workshop: Optional[Callable] = None,
        initial_slug: Optional[str] = None,
        initial_game_domain: Optional[str] = None,
        initial_revision: Optional[int] = None,
    ):
        super().__init__(parent, bg=BG_DEEP)
        self._game_domain = game_domain
        self._api = api
        self._game = game
        self._app_root = app_root or parent.winfo_toplevel()
        self._log = log_fn or (lambda msg: None)
        self._on_close = on_close
        self._on_open_workshop = on_open_workshop
        self._initial_slug = initial_slug
        self._initial_game_domain = (initial_game_domain or game_domain).lower()
        self._initial_revision = initial_revision

        self._collections: list = []
        self._cards: list[CollectionCard] = []
        self._page: int = 0
        self._loading: bool = False
        self._search_active: bool = False
        self._img_cache: dict = {}
        self._img_loading: set = set()
        self._cols: int = _COLL_COLS
        self._loader: CTkLoader | None = None

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
        self._open_detail(col, revision_number=self._initial_revision)

    @staticmethod
    def _parse_collection_url(url: str) -> tuple[str, str, int | None]:
        """
        Extract (slug, game_domain, revision_number) from a Nexus Mods collection URL.

        Handles patterns like:
          https://www.nexusmods.com/skyrimspecialedition/collections/x2ezso
          https://www.nexusmods.com/games/skyrimspecialedition/collections/x2ezso
          https://next.nexusmods.com/skyrimspecialedition/collections/x2ezso
          https://www.nexusmods.com/games/stardewvalley/collections/tckf0m/revisions/97
        Returns ('', '', None) if parsing fails.
        """
        m = re.search(
            r'nexusmods\.com/(?:games/)?([^/?#]+)/collections/([A-Za-z0-9_\-]+)'
            r'(?:/revisions/(\d+))?',
            url,
        )
        if m:
            rev = int(m.group(3)) if m.group(3) else None
            return m.group(2), m.group(1), rev
        return '', '', None

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

        self._workshop_btn = ctk.CTkButton(
            toolbar, text="Workshop", width=scaled(90), height=scaled(26),
            fg_color="#7b2fa8", hover_color="#9b3fd0", text_color="white",
            font=FONT_HEADER, command=self._open_workshop,
        )
        self._workshop_btn.pack(side="left", padx=4, pady=2)

        self._import_manifest_btn = ctk.CTkButton(
            toolbar, text="Import Manifest", width=scaled(115), height=scaled(26),
            fg_color="#2a6e3f", hover_color="#369150", text_color="white",
            font=FONT_HEADER, command=self._import_manifest,
        )
        self._import_manifest_btn.pack(side="left", padx=4, pady=2)

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
        self._canvas_frame = canvas_frame = tk.Frame(self, bg=BG_DEEP, bd=0, highlightthickness=0)
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
        slug, url_domain, revision_number = self._parse_collection_url(self._open_current_url)
        if not slug:
            return
        game_domain = url_domain or self._game_domain
        from Nexus.nexus_api import NexusCollection
        col = NexusCollection(slug=slug, name=slug, game_domain=game_domain)
        # Pass current profile dir so Reset Load Order button appears (profile name may differ from slug)
        profile_dir = getattr(self._game, "_active_profile_dir", None) if self._game else None
        self._open_detail(col, profile_dir=profile_dir, revision_number=revision_number)

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

        slug, url_domain, revision_number = self._parse_collection_url(url)
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
        self._open_detail(col, revision_number=revision_number)

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

    def _find_installed_profile_dir(self, slug: str) -> Path | None:
        """Return the profile dir that has this collection's slug installed, or None."""
        if not self._game or not slug:
            return None
        try:
            profiles_root = self._game.get_profile_root() / "profiles"
            if not profiles_root.is_dir():
                return None
            for profile_dir in profiles_root.iterdir():
                if not profile_dir.is_dir():
                    continue
                url = get_collection_url_from_profile(profile_dir)
                if not url:
                    continue
                installed_slug, _, _ = self._parse_collection_url(url)
                if installed_slug and installed_slug.lower() == slug.lower():
                    return profile_dir
        except Exception:
            pass
        return None

    def _build_cards(self):
        self._clear_cards()
        for col in self._collections:
            profile_dir = self._find_installed_profile_dir(col.slug)
            card = CollectionCard(
                self._inner, col,
                on_view=lambda c=col, pd=profile_dir: self._open_detail(c, profile_dir=pd),
            )
            self._bind_scroll(card.card)
            self._cards.append(card)
        self._regrid_cards()
        self._load_images_then_hide_loader()

    def _open_detail(self, collection, profile_dir=None, revision_number=None, local_manifest_path=None):
        self._close_detail()
        panel = CollectionDetailDialog(
            self, collection=collection,
            game_domain=self._game_domain, api=self._api,
            game=self._game, app_root=self._app_root, log_fn=self._log,
            on_close=self._close_detail,
            profile_dir=profile_dir,
            revision_number=revision_number,
            local_manifest_path=local_manifest_path,
        )
        panel.place(relx=0, rely=0, relwidth=1, relheight=1)
        self._detail_panel = panel

    def _import_manifest(self):
        from Utils.portal_filechooser import _run_file_picker_worker
        filters = [("JSON manifest", ["*.json"]), ("All files", ["*"])]
        threading.Thread(
            target=_run_file_picker_worker,
            args=("Import Manifest", filters, lambda p: self.after(0, lambda: self._on_manifest_picked(p))),
            daemon=True,
        ).start()

    def _on_manifest_picked(self, path):
        if not path:
            return
        import json as _json
        try:
            cj = _json.loads(Path(path).read_text(encoding="utf-8"))
        except Exception as exc:
            tk.messagebox.showerror("Import Manifest", f"Could not read manifest:\n{exc}", parent=self)
            return
        from Nexus.nexus_api import NexusCollection as _NC
        col_name = (cj.get("info") or {}).get("name") or Path(path).stem
        game_domain = (cj.get("info") or {}).get("domainName") or self._game_domain
        col = _NC(name=col_name, slug="", game_domain=game_domain)
        self._open_detail(col, local_manifest_path=str(path))

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

    def _load_images_then_hide_loader(self):
        """Kick off async image loads; hide the loader once all images are done."""
        cards = list(self._cards)
        if not cards:
            self._hide_loader()
            return

        pending_urls: set[str] = set()
        pending_count = 0
        for card in cards:
            url = card._collection.tile_image_url or ""
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

        for card in cards:
            url = card._collection.tile_image_url or ""
            needs_fetch = url and url not in self._img_cache and url in pending_urls
            card.load_image_async(
                url,
                self._img_cache,
                self._img_loading,
                self,
                on_done=_on_image_done if needs_fetch else None,
            )
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
        self._show_loader()

        def _worker():
            try:
                cols = self._api.get_collections(
                    self._game_domain, count=PAGE_SIZE, offset=page * PAGE_SIZE
                )
                self.after(0, lambda: self._on_loaded(cols, page, search=False))
            except Exception as exc:
                self.after(0, lambda e=exc: self._on_error(e))

        threading.Thread(target=_worker, daemon=True).start()

    def _on_loaded(self, cols: list, page: int, search: bool):
        try:
            if not self.winfo_exists():
                return
        except Exception:
            return
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
        try:
            if not self.winfo_exists():
                return
        except Exception:
            return
        self._hide_loader()
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
        self._show_loader()

        def _worker():
            try:
                cols = self._api.search_collections(
                    self._game_domain, query_text, count=PAGE_SIZE, offset=0
                )
                self.after(0, lambda: self._on_search_done(cols, query_text))
            except Exception as exc:
                self.after(0, lambda e=exc: self._on_search_error(e))

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
        self._hide_loader()
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

    # ------------------------------------------------------------------
    # Workshop
    # ------------------------------------------------------------------

    def _open_workshop(self):
        if not self._game:
            tk.messagebox.showerror("Workshop", "No game selected.", parent=self)
            return
        profile_dir = getattr(self._game, "_active_profile_dir", None)
        if not profile_dir:
            tk.messagebox.showerror("Workshop", "No active profile.", parent=self)
            return
        modlist_path = Path(profile_dir) / "modlist.txt"
        if not modlist_path.is_file():
            tk.messagebox.showerror("Workshop", f"modlist.txt not found:\n{modlist_path}", parent=self)
            return

        entries = [
            e for e in reversed(read_modlist(modlist_path))
            if e.enabled and not e.is_separator
        ]

        if self._on_open_workshop:
            self._on_open_workshop(entries)
