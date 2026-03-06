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
from Utils.modlist import write_modlist, ModEntry

# Collections-specific card dimensions (5-column grid)
_COLL_COLS  = 5
_COLL_W     = 200
_COLL_IMG_W = 190
_COLL_IMG_H = 110
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
_SUMMARY_MAX = 80


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

        # Outer card frame
        self.card = ctk.CTkFrame(
            parent,
            width=_COLL_W, height=320,
            fg_color=BG_PANEL,
            border_color=BORDER, border_width=1,
            corner_radius=8,
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

        # Name
        name_text = col.name or f"Collection {col.id}"
        ctk.CTkLabel(
            self.card, text=name_text,
            font=FONT_BOLD, text_color=TEXT_MAIN,
            wraplength=_COLL_W - 16, justify="left", anchor="w",
        ).pack(padx=8, fill="x")

        # Stats: downloads, endorsements, mod count
        stats = f"↓{col.total_downloads:,}  ♥{col.endorsements:,}  {col.mod_count} mods"
        ctk.CTkLabel(
            self.card, text=stats,
            font=FONT_SMALL, text_color=TEXT_DIM,
            anchor="w",
        ).pack(padx=8, fill="x")

        # Author
        if col.user_name:
            ctk.CTkLabel(
                self.card, text=f"by {col.user_name}",
                font=FONT_SMALL, text_color=TEXT_DIM,
                anchor="w",
            ).pack(padx=8, fill="x")

        # Summary
        summary = (col.summary or "").strip()
        if len(summary) > _SUMMARY_MAX:
            summary = summary[:_SUMMARY_MAX].rstrip() + "…"
        if summary:
            ctk.CTkLabel(
                self.card, text=summary,
                font=FONT_SMALL, text_color=TEXT_DIM,
                wraplength=_COLL_W - 16, justify="left", anchor="w",
            ).pack(padx=8, pady=(2, 0), fill="x")

        # Button row
        ctk.CTkButton(
            self.card, text="View",
            height=28, fg_color=ACCENT, hover_color=ACCENT_HOV,
            text_color="#ffffff", font=FONT_SMALL,
            command=on_view,
        ).pack(padx=10, pady=(6, 8), fill="x", side="bottom")

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
            self._tree.insert(
                "", "end",
                values=(order_label, mod.mod_name, mod.mod_author, mod.file_name,
                        _fmt_size(mod.size_bytes), opt_mark),
                tags=(tag,),
            )

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
        already_installed_by_fid: dict[int, str] = {}  # file_id → staging folder name
        staging_lower_map: dict[str, str] = {}          # lower(name) → actual name
        if staging_path.exists():
            import configparser as _cp
            for mod_dir in staging_path.iterdir():
                if not mod_dir.is_dir():
                    continue
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
            with _cf.ThreadPoolExecutor(max_workers=_DL_WORKERS) as _pool:
                list(_pool.map(_download_one, to_download))

        # ------------------------------------------------------------------
        # Step 2b: Install downloaded archives sequentially (main thread)
        # ------------------------------------------------------------------
        for seq_idx, mod in enumerate(to_download, 1):
            if not self.winfo_exists():
                break

            result = _dl_results.get(mod.file_id)
            if result is None or not result.success or not result.file_path:
                self._log(f"Collection install: download failed for '{mod.mod_name}'")
                skipped += 1
                continue

            sort_key = _sort_key(mod)
            try:
                self.after(0, lambda m=mod, i=seq_idx, t=_dl_total: self._status_var.set(
                    f"Installing {i}/{t}: {m.mod_name}\u2026"
                ))
            except Exception:
                break

            # Snapshot staging dir to detect the newly-installed folder
            before_folders: set[str] = set()
            if staging_path.exists():
                try:
                    before_folders = {p.name for p in staging_path.iterdir() if p.is_dir()}
                except Exception:
                    pass

            # Install on the main thread, wait for it to finish
            done_event = threading.Event()
            archive_path = str(result.file_path)
            auto_fomod = fomod_by_file_id.get(mod.file_id)

            def _do_install(ap=archive_path, cm=mod, af=auto_fomod):
                try:
                    install_mod_from_archive(
                        ap, self, self._log, self._game,
                        fomod_auto_selections=af,
                    )
                except Exception as exc:
                    self._log(f"Collection install: install failed for '{cm.mod_name}': {exc}")
                finally:
                    done_event.set()

            try:
                self.after(0, _do_install)
            except Exception:
                done_event.set()

            done_event.wait(timeout=600)  # 10 min max per mod (FOMOD + extract)

            # Remove the downloaded archive now that it has been installed
            try:
                Path(archive_path).unlink(missing_ok=True)
            except Exception as _del_exc:
                self._log(f"Collection install: could not remove archive '{archive_path}': {_del_exc}")

            # Detect what folder was created
            new_folder: str = ""
            if staging_path.exists():
                try:
                    after_folders = {p.name for p in staging_path.iterdir() if p.is_dir()}
                    new_dirs = after_folders - before_folders
                    if new_dirs:
                        new_folder = next(iter(new_dirs))
                except Exception:
                    pass
            if not new_folder:
                # Fallback: use the mod name from collection.json or GraphQL
                new_folder = schema_pos_to_name.get(sort_key) or mod.mod_name

            install_order.append((sort_key, new_folder))
            installed += 1

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

    def _build(self):
        self.grid_rowconfigure(1, weight=1)
        self.grid_rowconfigure(2, weight=0)
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

        self._status_label = tk.Label(
            toolbar, text="Loading collections…",
            font=FONT_SMALL, fg=TEXT_DIM, bg=BG_HEADER, anchor="w",
        )
        self._status_label.pack(side="left", padx=8, fill="x", expand=True)

        # Scrollable card canvas
        canvas_frame = tk.Frame(self, bg=BG_DEEP, bd=0, highlightthickness=0)
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
        for w in (self._canvas, self._inner):
            w.bind("<Button-4>",   lambda e: self._scroll(-80))
            w.bind("<Button-5>",   lambda e: self._scroll(80))
            w.bind("<MouseWheel>", self._on_mousewheel)

        # Search bar
        search_bar = tk.Frame(self, bg=BG_HEADER, height=34)
        search_bar.grid(row=2, column=0, sticky="ew")
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
    # Canvas / scroll helpers
    # ------------------------------------------------------------------

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
        total_card_w = self._cols * _COLL_W + (self._cols - 1) * CARD_PAD
        canvas_w = self._canvas.winfo_width() or (self._cols * (_COLL_W + CARD_PAD * 2))
        x_pad = max(CARD_PAD, (canvas_w - total_card_w) // 2)

        for idx, c in enumerate(self._cards):
            col = idx % self._cols
            row = idx // self._cols
            c.card.grid(
                row=row, column=col,
                padx=(x_pad if col == 0 else CARD_PAD // 2,
                       x_pad if col == self._cols - 1 else CARD_PAD // 2),
                pady=CARD_PAD,
                sticky="n",
            )
        for c in range(self._cols):
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
