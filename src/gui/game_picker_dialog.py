"""
Game picker dialog — card grid for selecting/adding games.

Extracted from dialogs.py to keep the main dialogs module focused.
"""

import threading
import tkinter as tk
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from PIL import Image as _PilImage, ImageTk as _PilTk

import customtkinter as ctk

from gui.wheel_compat import LEGACY_WHEEL_REDUNDANT
from gui.theme import (
    ACCENT,
    ACCENT_HOV,
    TEXT_ON_ACCENT,
    BG_DEEP,
    BG_HEADER,
    BG_PANEL,
    BORDER,
    FONT_BOLD,
    FONT_NORMAL,
    font_sized_px,
    scaled,
    TEXT_DIM,
    TEXT_MAIN,
)
import gui.theme as _theme
from gui.ctk_components import CTkAlert, CTkLoader
from Utils.config_paths import get_custom_game_images_dir, get_custom_games_dir


_CUSTOM_HANDLERS_API_URL = (
    "https://api.github.com/repos/ChrisDKN/Amethyst-Mod-Manager/contents/"
    "Custom%20Handlers?ref=main"
)


def _center_crop_to_square(img: "_PilImage.Image", size: int) -> "_PilImage.Image":
    """Scale image to cover a size×size square, then center-crop. Returns square PIL Image."""
    src_w, src_h = img.size
    scale = max(size / src_w, size / src_h)
    new_w, new_h = int(src_w * scale), int(src_h * scale)
    resample = _PilImage.Resampling.LANCZOS if hasattr(_PilImage, "Resampling") else _PilImage.LANCZOS  # type: ignore
    img = img.resize((new_w, new_h), resample)
    x_off = (new_w - size) // 2
    y_off = (new_h - size) // 2
    return img.crop((x_off, y_off, x_off + size, y_off + size))


# Session-level cache: game_id → PhotoImage (keyed by (game_id, pixel_size))
_IMAGE_CACHE: dict[tuple[str, int], "_PilTk.PhotoImage"] = {}
# Shared executor for async card image loading (daemon threads)
_IMG_EXECUTOR = ThreadPoolExecutor(max_workers=4, thread_name_prefix="card_img")


def _load_card_image_sync(img_path: Path, img_sq: int, px_size: int) -> "_PilImage.Image | None":
    """Load, crop, and resize a game card image. Returns a PIL Image (not PhotoImage).
    Safe to call from a worker thread; PhotoImage must be created on the main thread."""
    try:
        raw = _PilImage.open(img_path).convert("RGBA")
        raw = _center_crop_to_square(raw, img_sq)
        raw = raw.resize((px_size, px_size), _PilImage.Resampling.LANCZOS if hasattr(_PilImage, "Resampling") else _PilImage.LANCZOS)  # type: ignore
        return raw.convert("RGB")
    except Exception:
        return None


class _GamePickerDialog(ctk.CTkToplevel):
    """Thin modal wrapper around GamePickerPanel.

    Callers access ``result`` (str | None) and ``selected_only`` (bool).
    """

    def __init__(self, parent, game_names: list[str], games: dict | None = None,
                 show_download_custom_handler_fn=None):
        super().__init__(parent, fg_color=BG_DEEP)
        self.title("Add Game")
        self.resizable(True, True)
        self.transient(parent)
        self.protocol("WM_DELETE_WINDOW", self._cancel)

        self.result: str | None = None
        self.selected_only: bool = False

        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(0, weight=1)

        def _on_selected(name: str, already_configured: bool):
            self.result = name
            self.selected_only = already_configured
            self._cancel()

        self._panel = GamePickerPanel(
            self,
            game_names,
            games=games,
            on_game_selected=_on_selected,
            on_cancel=self._cancel,
            show_download_custom_handler_fn=show_download_custom_handler_fn,
        )
        self._panel.grid(row=0, column=0, sticky="nsew")

        _COLS = 4
        _PAD = 6
        _CARD_W = 175
        _CARD_H = 200
        _pad = scaled(_PAD)
        slot_w = scaled(_CARD_W + _PAD * 2)
        slot_h = scaled(_CARD_H + _PAD)
        w = _COLS * slot_w + _pad + 8
        rows_count = (len(game_names) + _COLS - 1) // _COLS
        content_h = rows_count * slot_h + _pad
        h = min(max(300, content_h + 120), 700)
        x = parent.winfo_rootx() + (parent.winfo_width() - w) // 2
        y = parent.winfo_rooty() + (parent.winfo_height() - h) // 2
        self.geometry(f"{w}x{h}+{x}+{y}")

        self.after(50, self._make_modal)

    def _make_modal(self):
        try:
            self.grab_set()
            self.focus_set()
        except Exception:
            pass

    def _cancel(self):
        try:
            self.grab_release()
        except Exception:
            pass
        self.destroy()

class GamePickerPanel(tk.Frame):
    """
    Game-picker card grid that embeds directly in the main window (no Toplevel).

    Placed over the main content area with ``place(relx=0, rely=0,
    relwidth=1, relheight=1)``.  Columns reflow automatically when the
    window is resized, matching the behaviour of the Collections browser.

    Callbacks
    ---------
    on_game_selected(name: str, already_configured: bool)
        Called when the user clicks "Add" or "Select" on a card.
    on_cancel()
        Called when the user clicks the "✕ Cancel" / Close button.
    """

    _CARD_W = 175
    _CARD_H = 200
    _IMG_H  = 130
    _IMG_SQ = 130   # square image slot — no scale() to avoid layout/clipping issues
    _PAD    = 6     # smaller gap so rightmost card isn't cut off at high UI scale

    def __init__(
        self,
        parent: tk.Widget,
        game_names: list,
        games: dict | None = None,
        on_game_selected=None,
        on_cancel=None,
        show_custom_game_panel_fn=None,
        show_download_custom_handler_fn=None,
    ):
        super().__init__(parent, bg=BG_DEEP)
        self._game_names = game_names
        self._games = games or {}
        self._icons_dir = Path(__file__).resolve().parent.parent / "icons" / "games"
        self._on_game_selected = on_game_selected or (lambda n, c: None)
        self._on_cancel = on_cancel or (lambda: None)
        self._show_custom_game_panel_fn = show_custom_game_panel_fn
        self._show_download_custom_handler_fn = show_download_custom_handler_fn

        self._img_refs: list = []
        self._img_labels: dict = {}           # game_id → (img_lbl, img_frame)
        self._card_widgets: list = []          # list of card frames (in order)
        self._card_names: list[str] = []       # parallel list: game name per card
        self._curr_cols: int = 4
        self._show_installed_only = tk.BooleanVar(value=False)
        self._installed_game_names: set[str] | None = None  # None = not yet scanned
        self._loader: CTkLoader | None = None
        self._remote_handlers: list[dict] = []  # parsed remote handler dicts not yet downloaded

        self._build()

        # Download banner images for custom games in background
        try:
            from Games.Custom.custom_game import download_missing_custom_game_images
            download_missing_custom_game_images(on_done=self._on_image_downloaded)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Build
    # ------------------------------------------------------------------

    def _build(self):
        self.grid_rowconfigure(0, weight=0)   # title bar
        self.grid_rowconfigure(1, weight=0)   # subtitle
        self.grid_rowconfigure(2, weight=1)   # canvas
        self.grid_rowconfigure(3, weight=0)   # button bar
        self.grid_columnconfigure(0, weight=1)

        # ---- Title bar ----
        title_bar = tk.Frame(self, bg=BG_HEADER, height=scaled(40))
        title_bar.grid(row=0, column=0, sticky="ew")
        title_bar.grid_propagate(False)

        tk.Label(
            title_bar, text="Add Game",
            font=font_sized_px(_theme.FONT_FAMILY, 14, "bold"), fg=TEXT_MAIN, bg=BG_HEADER, anchor="w",
        ).pack(side="left", padx=scaled(12), pady=scaled(8))

        tk.Button(
            title_bar, text="✕  Cancel",
            bg="#6b3333", fg="#ffffff", activebackground="#8c4444",
            activeforeground="#ffffff",
            relief="flat", font=font_sized_px(_theme.FONT_FAMILY, 12),
            bd=0, highlightthickness=0, cursor="hand2",
            command=self._on_cancel,
        ).pack(side="right", padx=(scaled(4), scaled(12)), pady=scaled(6))

        # Separator under title bar
        tk.Frame(self, bg=BORDER, height=1).grid(row=0, column=0, sticky="ews")

        # ---- Subtitle ----
        tk.Label(
            self, text="Select a game to add:",
            font=font_sized_px(_theme.FONT_FAMILY, 14, "bold"), fg=TEXT_MAIN, bg=BG_DEEP, anchor="w",
        ).grid(row=1, column=0, sticky="ew", padx=scaled(16), pady=(scaled(8), scaled(2)))

        # ---- Scrollable canvas ----
        self._canvas_frame = canvas_frame = tk.Frame(self, bg=BG_DEEP, bd=0, highlightthickness=0)
        canvas_frame.grid(row=2, column=0, sticky="nsew")
        canvas_frame.grid_rowconfigure(0, weight=1)
        canvas_frame.grid_rowconfigure(1, weight=0)
        canvas_frame.grid_columnconfigure(0, weight=1)

        self._canvas = tk.Canvas(
            canvas_frame, bg=BG_DEEP, bd=0,
            highlightthickness=0, yscrollincrement=4, xscrollincrement=4, takefocus=0,
        )
        vsb = tk.Scrollbar(
            canvas_frame, orient="vertical", command=self._canvas.yview,
            bg="#383838", troughcolor=BG_DEEP, activebackground=ACCENT,
            highlightthickness=0, bd=0,
        )
        hsb = tk.Scrollbar(
            canvas_frame, orient="horizontal", command=self._canvas.xview,
            bg="#383838", troughcolor=BG_DEEP, activebackground=ACCENT,
            highlightthickness=0, bd=0,
        )
        self._canvas.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        self._canvas.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")

        self._inner = ctk.CTkFrame(self._canvas, fg_color=BG_DEEP)
        self._inner_id = self._canvas.create_window(
            (0, 0), window=self._inner, anchor="nw"
        )

        self._inner.bind("<Configure>", self._on_inner_configure)
        self._canvas.bind("<Configure>", self._on_canvas_configure)

        # Forward scroll from anywhere in this overlay to the canvas.
        # Linux: Button-4/5; Flatpak/Wayland: may send MouseWheel — handle both.
        # Shift+wheel scrolls horizontally when content overflows.
        def _fwd_scroll(e):
            num = getattr(e, "num", None)
            delta = getattr(e, "delta", 0) or 0
            up = num == 4 or delta > 0
            if getattr(e, "state", 0) & 0x1:  # Shift held: horizontal scroll
                self._canvas.xview_scroll(-50 if up else 50, "units")
            else:
                self._canvas.yview_scroll(-50 if up else 50, "units")
        self._fwd_scroll = _fwd_scroll
        # Bind on the canvas and inner frame directly — scroll events are delivered
        # to the widget under the cursor, not the ancestor frame.
        for _w in (self._canvas, self._inner):
            if not LEGACY_WHEEL_REDUNDANT:
                _w.bind("<Button-4>",   _fwd_scroll, add="+")
                _w.bind("<Button-5>",   _fwd_scroll, add="+")
            _w.bind("<MouseWheel>", _fwd_scroll, add="+")
        self.bind("<Destroy>", self._on_destroy)

        # Show loader while cards are being built
        self._loader = CTkLoader(canvas_frame)

        def _deferred_build():
            try:
                for name in self._game_names:
                    try:
                        self._build_card(name)
                    except Exception:
                        pass
                try:
                    self._regrid_cards()
                except Exception:
                    pass
            finally:
                # Always drop the loader — a stuck spinner is the worst failure mode.
                self._hide_loader()
                # Start fetching remote handlers after local cards are shown
                threading.Thread(target=self._fetch_remote_handlers, daemon=True).start()

        self.after(50, _deferred_build)

        # ---- Bottom button bar ----
        btn_bar = ctk.CTkFrame(self, fg_color=BG_PANEL, corner_radius=0, height=52)
        btn_bar.grid(row=3, column=0, sticky="ew")
        btn_bar.grid_propagate(False)
        ctk.CTkFrame(btn_bar, fg_color=BORDER, height=1, corner_radius=0).pack(
            side="top", fill="x"
        )
        ctk.CTkButton(
            btn_bar, text="+ Define Custom Game",
            width=170, height=30, font=FONT_BOLD,
            fg_color="#2d7a2d", hover_color="#3a9a3a", text_color="white",
            command=self._on_define_custom_game,
        ).pack(side="left", padx=(12, 4), pady=10)
        ctk.CTkCheckBox(
            btn_bar, text="Show only installed",
            variable=self._show_installed_only,
            font=FONT_NORMAL, text_color=TEXT_MAIN,
            fg_color=ACCENT, hover_color=ACCENT_HOV,
            command=self._on_installed_filter_toggle,
        ).pack(side="left", padx=(8, 4), pady=10)

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
    # Card building
    # ------------------------------------------------------------------

    def _build_card(self, name: str):
        game    = self._games.get(name)
        game_id = game.game_id if game else name.lower().replace(" ", "_")
        _img_sz = scaled(self._IMG_SQ)  # image scales with UI (tk.Frame uses actual pixels)
        # CTk scales widget dimensions; use unscaled design values so we scale once (like browse panel)
        card = ctk.CTkFrame(
            self._inner,
            fg_color=BG_PANEL,
            corner_radius=8,
            border_width=1,
            border_color=BORDER,
            width=self._CARD_W,
            height=self._CARD_H,
        )
        card.grid_propagate(False)
        card.grid_columnconfigure(0, weight=1)
        card.grid_rowconfigure(0, weight=0, minsize=_img_sz)
        card.grid_rowconfigure(1, weight=0, minsize=32)
        card.grid_rowconfigure(2, weight=0)

        img_frame = tk.Frame(card, bg=BG_DEEP, width=_img_sz, height=_img_sz)
        img_frame.grid(row=0, column=0, padx=4, pady=(4, 0), sticky="n")
        img_frame.pack_propagate(False)

        img_path = self._icons_dir / f"{game_id}.png"
        if not img_path.is_file():
            img_path = self._icons_dir / f"{game_id.lower()}.png"
        if not img_path.is_file():
            from Utils.config_paths import get_custom_game_images_dir as _gcgid
            custom_img = _gcgid() / f"{game_id}.png"
            if custom_img.is_file():
                img_path = custom_img

        if img_path.is_file():
            raw = _PilImage.open(img_path).convert("RGBA")
            raw = _center_crop_to_square(raw, self._IMG_SQ)
            raw = raw.resize((_img_sz, _img_sz), _PilImage.Resampling.LANCZOS if hasattr(_PilImage, "Resampling") else _PilImage.LANCZOS)  # type: ignore
            photo = _PilTk.PhotoImage(raw.convert("RGB"))
            self._img_refs.append(photo)
            img_lbl = tk.Label(img_frame, image=photo, bg=BG_DEEP)
        else:
            img_lbl = tk.Label(img_frame, text="?", font=(_theme.FONT_FAMILY, 36, "bold"),
                              fg=TEXT_DIM, bg=BG_DEEP)
        img_lbl.place(relx=0.5, rely=0.5, anchor="center")
        self._img_labels[game_id] = (img_lbl, img_frame)

        ctk.CTkLabel(
            card, text=name,
            font=(_theme.FONT_FAMILY, 12, "bold"), text_color=TEXT_MAIN,
            wraplength=scaled(self._CARD_W - 8), anchor="center", justify="center",
        ).grid(row=1, column=0, padx=4, pady=(4, 2), sticky="ew")

        is_configured = bool(game and game.is_configured())

        def _select(n=name, already=is_configured):
            self._on_game_selected(n, already)

        btn_text  = "Select" if is_configured else "Add"
        btn_fg    = "#2d7a2d" if is_configured else ACCENT
        btn_hover = "#3a9a3a" if is_configured else ACCENT_HOV

        ctk.CTkButton(
            card, text=btn_text, height=26, font=FONT_BOLD,
            fg_color=btn_fg, hover_color=btn_hover, text_color="white",
            command=_select,
        ).grid(row=2, column=0, padx=8, pady=(0, 8), sticky="ew")

        # Hover highlight + forward scroll from every card widget to the canvas
        def _enter(e, c=card): c.configure(border_color=ACCENT)
        def _leave(e, c=card): c.configure(border_color=BORDER)
        def _bind_card_widget(w):
            w.bind("<Enter>", _enter, add="+")
            w.bind("<Leave>", _leave, add="+")
            if not LEGACY_WHEEL_REDUNDANT:
                w.bind("<Button-4>",   self._fwd_scroll, add="+")
                w.bind("<Button-5>",   self._fwd_scroll, add="+")
            w.bind("<MouseWheel>", self._fwd_scroll, add="+")
            for child in w.winfo_children():
                _bind_card_widget(child)
        _bind_card_widget(card)

        self._card_widgets.append(card)
        self._card_names.append(name)

    # ------------------------------------------------------------------
    # Scroll helpers
    # ------------------------------------------------------------------

    def _on_destroy(self, event):
        if event.widget is self:
            try:
                for _w in (self._canvas, self._inner):
                    if not LEGACY_WHEEL_REDUNDANT:
                        _w.unbind("<Button-4>")
                        _w.unbind("<Button-5>")
                    _w.unbind("<MouseWheel>")
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Column-reflow (mirrors CollectionsDialog._regrid_cards)
    # ------------------------------------------------------------------

    def _on_inner_configure(self, _event=None):
        if getattr(self, "_inner_sr_syncing", False):
            return
        self._inner_sr_syncing = True
        try:
            bbox = self._canvas.bbox("all")
            if bbox:
                x1, y1, x2, y2 = bbox
                cw = self._canvas.winfo_width() or 1
                ch = self._canvas.winfo_height() or 1
                scr_w = max(x2, cw + 1)
                scr_h = max(y2 - y1, ch + 1)
                new_sr = (0, 0, scr_w, scr_h)
                if new_sr != getattr(self, "_inner_sr_applied", None):
                    self._canvas.configure(scrollregion=new_sr)
                    self._inner_sr_applied = new_sr
        finally:
            self._inner_sr_syncing = False

    def _on_canvas_configure(self, event):
        if hasattr(self, '_regrid_after_id') and self._regrid_after_id:
            self.after_cancel(self._regrid_after_id)
        self._regrid_after_id = self.after(250, self._regrid_cards)

    def _regrid_cards(self):
        # Layout uses scaled slot to match browse panel: slot = (card + padding) * ui_scale.
        # CTk scales card dimensions; add small buffer so rightmost card isn't clipped at high scale.
        _pad = scaled(self._PAD)
        _slack = scaled(4)  # extra px per slot to prevent rightmost card clipping
        slot_w = scaled(self._CARD_W + self._PAD * 2) + _slack
        canvas_w = self._canvas.winfo_width() or (self._curr_cols * slot_w)
        # Margin when wide (avoid too many cols); no margin when narrow (allow h-scroll)
        margin = scaled(24) if canvas_w > slot_w * 2 else 0
        avail_w = max(slot_w, canvas_w - margin)
        cols = max(1, avail_w // slot_w)
        self._curr_cols = cols

        # Inner width = content only; center via canvas coords (prevents column squeeze/clipping)
        content_w = cols * slot_w
        self._canvas.itemconfig(self._inner_id, width=content_w)
        x_off = max(0, (canvas_w - content_w) // 2)
        self._canvas.coords(self._inner_id, x_off, 0)

        for c in range(cols):
            self._inner.grid_columnconfigure(c, weight=0, minsize=slot_w)

        # Determine which cards are visible (installed filter)
        installed = self._installed_game_names
        filter_on = self._show_installed_only.get() and installed is not None
        visible_idx = 0
        for i, (card, name) in enumerate(zip(self._card_widgets, self._card_names)):
            if filter_on and name not in installed:
                card.grid_remove()
            else:
                col = visible_idx % cols
                row = visible_idx // cols
                card.grid(
                    row=row, column=col,
                    padx=_pad,
                    pady=_pad,
                    sticky="n",
                )
                visible_idx += 1

        # Ensure scroll region allows horizontal scroll when content exceeds viewport
        self._canvas.update_idletasks()
        cw = self._canvas.winfo_width() or 1
        ch = self._canvas.winfo_height() or 1
        # scr_w must exceed cw when content is wider, so horizontal scrollbar activates
        scr_w = max(content_w + x_off, cw + 1)
        bbox = self._canvas.bbox("all")
        scr_h = (bbox[3] - bbox[1]) if bbox else max(1000, ch + 1)
        scr_h = max(scr_h, ch + 1)
        self._canvas.configure(scrollregion=(0, 0, scr_w, scr_h))

    # ------------------------------------------------------------------
    # Live image updates (custom games)
    # ------------------------------------------------------------------

    def _on_image_downloaded(self, game_id: str) -> None:
        """Called from a worker thread when a banner image has been cached."""
        def _apply():
            entry = self._img_labels.get(game_id)
            if entry is None:
                return
            img_lbl, img_frame = entry
            try:
                if not img_lbl.winfo_exists():
                    return
            except Exception:
                return
            from Utils.config_paths import get_custom_game_images_dir as _gcgid
            img_path = _gcgid() / f"{game_id}.png"
            if not img_path.is_file():
                return
            try:
                raw = _PilImage.open(img_path).convert("RGBA")
                raw = _center_crop_to_square(raw, self._IMG_SQ)
                sz = scaled(self._IMG_SQ)
                raw = raw.resize((sz, sz), _PilImage.Resampling.LANCZOS if hasattr(_PilImage, "Resampling") else _PilImage.LANCZOS)  # type: ignore
                photo = _PilTk.PhotoImage(raw.convert("RGB"))
                self._img_refs.append(photo)
                img_lbl.configure(image=photo, text="")
            except Exception:
                pass
        try:
            self.after(0, _apply)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Installed-only filter
    # ------------------------------------------------------------------

    def _on_installed_filter_toggle(self):
        if self._show_installed_only.get() and self._installed_game_names is None:
            # First activation: kick off background scan, show spinner text on checkbox
            import threading
            t = threading.Thread(target=self._scan_installed_games, daemon=True)
            t.start()
        else:
            self._regrid_cards()

    def _scan_installed_games(self):
        """Runs in a background thread. Detects installed games via Steam + Heroic."""
        try:
            from Utils.steam_finder import find_steam_libraries, find_game_by_steam_id, find_game_in_libraries
            from Utils.heroic_finder import find_heroic_game, find_heroic_game_info_by_exe
        except Exception:
            self.after(0, lambda: self._show_installed_only.set(False))
            return

        libraries = find_steam_libraries()
        installed: set[str] = set()

        for name, game in self._games.items():
            found = False
            all_exe = [game.exe_name] + list(getattr(game, "exe_name_alts", []))
            # Steam detection via app manifest (most reliable)
            steam_id = getattr(game, "steam_id", "")
            if steam_id and libraries:
                if find_game_by_steam_id(libraries, steam_id, game.exe_name):
                    found = True
            # Steam detection via exe scan (fallback / alt editions)
            if not found and libraries:
                for exe in all_exe:
                    if find_game_in_libraries(libraries, exe):
                        found = True
                        break
            # Heroic detection via heroic_app_names (if handler declares them)
            if not found:
                heroic_names = getattr(game, "heroic_app_names", [])
                if heroic_names and find_heroic_game(heroic_names):
                    found = True
            # Heroic detection via exe name scan (works even without heroic_app_names)
            if not found:
                for exe in all_exe:
                    bare_exe = exe.replace("\\", "/").rsplit("/", 1)[-1]
                    if find_heroic_game_info_by_exe(bare_exe):
                        found = True
                        break
            if found:
                installed.add(name)

        # Also check remote (not-yet-downloaded) handlers
        for h in self._remote_handlers:
            parsed = h.get("_parsed", {})
            if not isinstance(parsed, dict):
                continue
            display_name = h.get("_display_name", "")
            exe_name = parsed.get("exe_name", "")
            steam_id = parsed.get("steam_id", "")
            found = False
            if steam_id and libraries:
                if find_game_by_steam_id(libraries, steam_id, exe_name):
                    found = True
            if not found and exe_name and libraries:
                if find_game_in_libraries(libraries, exe_name):
                    found = True
            if not found and exe_name:
                bare_exe = exe_name.replace("\\", "/").rsplit("/", 1)[-1]
                if find_heroic_game_info_by_exe(bare_exe):
                    found = True
            if found:
                installed.add(display_name)

        def _apply():
            self._installed_game_names = installed
            if self._show_installed_only.get():
                self._regrid_cards()

        try:
            self.after(0, _apply)
        except Exception:
            pass

    def _rescan_remote_installed(self):
        """Check remote handlers against already-scanned install data and regrid."""
        try:
            from Utils.steam_finder import find_steam_libraries, find_game_by_steam_id, find_game_in_libraries
            from Utils.heroic_finder import find_heroic_game_info_by_exe
        except Exception:
            return
        libraries = find_steam_libraries()
        for h in self._remote_handlers:
            parsed = h.get("_parsed", {})
            if not isinstance(parsed, dict):
                continue
            display_name = h.get("_display_name", "")
            exe_name = parsed.get("exe_name", "")
            steam_id = parsed.get("steam_id", "")
            found = False
            if steam_id and libraries:
                if find_game_by_steam_id(libraries, steam_id, exe_name):
                    found = True
            if not found and exe_name and libraries:
                if find_game_in_libraries(libraries, exe_name):
                    found = True
            if not found and exe_name:
                bare_exe = exe_name.replace("\\", "/").rsplit("/", 1)[-1]
                if find_heroic_game_info_by_exe(bare_exe):
                    found = True
            if found:
                self._installed_game_names.add(display_name)
        self._regrid_cards()

    # ------------------------------------------------------------------
    # Custom game definition
    # ------------------------------------------------------------------

    def _on_define_custom_game(self):
        if self._show_custom_game_panel_fn:
            def _on_done(panel):
                if panel.saved_game is not None:
                    self._on_game_selected(panel.saved_game.name, False)
            self._show_custom_game_panel_fn(None, _on_done)
        else:
            from gui.custom_game_dialog import CustomGameDialog
            dlg = CustomGameDialog(self.winfo_toplevel())
            self.winfo_toplevel().wait_window(dlg)
            if dlg.saved_game is not None:
                self._on_game_selected(dlg.saved_game.name, False)

    def _on_download_custom_handler(self):
        if self._show_download_custom_handler_fn:
            self._show_download_custom_handler_fn()

    # ------------------------------------------------------------------
    # Remote handler cards (fetched from GitHub)
    # ------------------------------------------------------------------

    def _fetch_remote_handlers(self):
        """Background thread: fetch handler list from GitHub and build cards for any not yet downloaded."""
        import json as _json
        from Utils.gh_cache import fetch_text as _gh_fetch_text
        try:
            listing = _gh_fetch_text(
                _CUSTOM_HANDLERS_API_URL,
                timeout=15,
            )
            if listing is None:
                return
            data = _json.loads(listing)
            handlers = [e for e in data if isinstance(e, dict) and e.get("name", "").endswith(".json")]
            # Fetch display name from inside each JSON
            result = []
            for h in handlers:
                filename = h.get("name", "")
                download_url = h.get("download_url")
                if not download_url:
                    continue
                # Skip if already downloaded
                from Utils.config_paths import get_custom_games_dir as _gcgd
                if (_gcgd() / filename).exists():
                    continue
                display_name = filename.removesuffix(".json").replace("_", " ")
                try:
                    raw = _gh_fetch_text(
                        download_url,
                        accept="*/*",
                        timeout=10,
                        min_interval=1800,
                    )
                    if raw is None:
                        continue
                    parsed = _json.loads(raw)
                    if isinstance(parsed, dict) and parsed.get("name"):
                        display_name = parsed["name"]
                    h["_parsed"] = parsed
                    # Download banner image if the handler declares one
                    image_url = parsed.get("image_url", "").strip() if isinstance(parsed, dict) else ""
                    game_id = parsed.get("game_id", filename.removesuffix(".json")) if isinstance(parsed, dict) else filename.removesuffix(".json")
                    if image_url:
                        from Utils.config_paths import get_custom_game_images_dir as _gcgid
                        cached = _gcgid() / f"{game_id}.png"
                        if not cached.is_file():
                            try:
                                import requests as _requests
                                from PIL import Image as _PILImg
                                import io as _io
                                resp2 = _requests.get(image_url, timeout=15)
                                resp2.raise_for_status()
                                img = _PILImg.open(_io.BytesIO(resp2.content)).convert("RGBA")
                                _gcgid().mkdir(parents=True, exist_ok=True)
                                img.save(cached, "PNG")
                            except Exception:
                                pass
                except Exception:
                    pass
                h["_display_name"] = display_name
                result.append(h)
            if result:
                try:
                    self.after(0, lambda r=result: self._on_remote_handlers_loaded(r))
                except Exception:
                    pass
        except Exception:
            pass

    def _on_remote_handlers_loaded(self, handlers: list):
        try:
            self._inner.winfo_exists()
        except Exception:
            return
        if not self._inner.winfo_exists():
            return
        self._remote_handlers = handlers
        for h in handlers:
            self._build_remote_card(h)
        # Re-sort all cards alphabetically so remote/custom handlers
        # appear in the correct position rather than always at the end.
        paired = sorted(zip(self._card_names, self._card_widgets), key=lambda x: x[0].lower())
        self._card_names[:] = [n for n, _ in paired]
        self._card_widgets[:] = [w for _, w in paired]
        self._regrid_cards()
        # If the installed filter is already active and scan is done, re-run regrid
        # (scan may have finished before remote cards arrived)
        if self._show_installed_only.get() and self._installed_game_names is not None:
            self._rescan_remote_installed()

    def _build_remote_card(self, h: dict):
        """Build a card for a remote (not yet downloaded) custom handler."""
        display_name = h.get("_display_name", h.get("name", "").removesuffix(".json"))
        filename = h.get("name", "")
        download_url = h.get("download_url", "")
        parsed = h.get("_parsed", {})
        game_id = parsed.get("game_id", filename.removesuffix(".json").lower())

        _img_sz = scaled(self._IMG_SQ)
        card = ctk.CTkFrame(
            self._inner,
            fg_color=BG_PANEL,
            corner_radius=8,
            border_width=1,
            border_color="#555566",
            width=self._CARD_W,
            height=self._CARD_H,
        )
        card.grid_propagate(False)
        card.grid_columnconfigure(0, weight=1)
        card.grid_rowconfigure(0, weight=0, minsize=_img_sz)
        card.grid_rowconfigure(1, weight=0, minsize=32)
        card.grid_rowconfigure(2, weight=0)

        img_frame = tk.Frame(card, bg=BG_DEEP, width=_img_sz, height=_img_sz)
        img_frame.grid(row=0, column=0, padx=4, pady=(4, 0), sticky="n")
        img_frame.pack_propagate(False)

        # Try to find an icon — check custom game images dir first (from image_url download)
        from Utils.config_paths import get_custom_game_images_dir as _gcgid
        img_path = _gcgid() / f"{game_id}.png"
        if not img_path.is_file():
            img_path = self._icons_dir / f"{game_id}.png"
        if not img_path.is_file():
            img_path = self._icons_dir / f"{game_id.lower()}.png"
        if img_path.is_file():
            raw = _PilImage.open(img_path).convert("RGBA")
            raw = _center_crop_to_square(raw, self._IMG_SQ)
            raw = raw.resize((_img_sz, _img_sz), _PilImage.Resampling.LANCZOS if hasattr(_PilImage, "Resampling") else _PilImage.LANCZOS)  # type: ignore
            photo = _PilTk.PhotoImage(raw.convert("RGB"))
            self._img_refs.append(photo)
            img_lbl = tk.Label(img_frame, image=photo, bg=BG_DEEP)
        else:
            img_lbl = tk.Label(img_frame, text="↓", font=(_theme.FONT_FAMILY, 36, "bold"),
                               fg=TEXT_DIM, bg=BG_DEEP)
        img_lbl.place(relx=0.5, rely=0.5, anchor="center")

        ctk.CTkLabel(
            card, text=display_name,
            font=(_theme.FONT_FAMILY, 12, "bold"), text_color=TEXT_MAIN,
            wraplength=scaled(self._CARD_W - 8), anchor="center", justify="center",
        ).grid(row=1, column=0, padx=4, pady=(4, 2), sticky="ew")

        btn = ctk.CTkButton(
            card, text="Add", height=26, font=FONT_BOLD,
            fg_color=ACCENT, hover_color=ACCENT_HOV, text_color=TEXT_ON_ACCENT,
        )
        btn.grid(row=2, column=0, padx=8, pady=(0, 8), sticky="ew")

        def _on_add(b=btn, fn=filename, url=download_url, dn=display_name, c=card):
            b.configure(state="disabled", text="Downloading…")
            self._download_and_select(url, fn, dn, c)

        btn.configure(command=_on_add)

        def _enter(e, c=card): c.configure(border_color=ACCENT)
        def _leave(e, c=card): c.configure(border_color="#555566")
        def _bind_card_widget(w):
            w.bind("<Enter>", _enter, add="+")
            w.bind("<Leave>", _leave, add="+")
            if not LEGACY_WHEEL_REDUNDANT:
                w.bind("<Button-4>",   self._fwd_scroll, add="+")
                w.bind("<Button-5>",   self._fwd_scroll, add="+")
            w.bind("<MouseWheel>", self._fwd_scroll, add="+")
            for child in w.winfo_children():
                _bind_card_widget(child)
        _bind_card_widget(card)

        self._card_widgets.append(card)
        self._card_names.append(display_name)

    def _download_and_select(self, download_url: str, filename: str, display_name: str, card):
        """Download the handler JSON, save it, then trigger game selection."""
        import json as _json
        import urllib.request as _urllib

        def _do():
            try:
                req = _urllib.Request(download_url, headers={"User-Agent": "Amethyst-Mod-Manager"})
                with _urllib.urlopen(req, timeout=15) as resp:
                    data = resp.read().decode("utf-8", errors="replace")
                _json.loads(data)  # validate
                from Utils.config_paths import get_custom_games_dir as _gcgd
                dest = _gcgd() / filename
                dest.write_text(data, encoding="utf-8")
                self.after(0, lambda: self._on_handler_downloaded(display_name, card, None))
            except Exception as e:
                self.after(0, lambda err=str(e): self._on_handler_downloaded(display_name, card, err))

        threading.Thread(target=_do, daemon=True).start()

    def _on_handler_downloaded(self, display_name: str, card, err: str | None):
        if err:
            # Re-enable the button so user can retry
            for w in card.winfo_children():
                if isinstance(w, ctk.CTkButton):
                    w.configure(state="normal", text="Add")
            return
        # Reload games so the new handler is registered, then select it
        from gui.game_helpers import _load_games, _GAMES
        _load_games()
        self._on_game_selected(display_name, False)

    def refresh(self):
        """Reload game registry and rebuild cards (e.g. after downloading a custom handler)."""
        from gui.game_helpers import _load_games, _GAMES
        _load_games()  # Repopulates _GAMES including newly downloaded custom games
        self._game_names = sorted(_GAMES.keys())
        self._games = _GAMES
        # Clear existing cards
        for w in self._card_widgets:
            try:
                w.destroy()
            except Exception:
                pass
        self._card_widgets.clear()
        self._card_names.clear()
        self._img_labels.clear()
        self._img_refs.clear()
        # Rebuild cards
        for name in self._game_names:
            self._build_card(name)
        self._regrid_cards()
        # Restart banner image downloads for any new custom games
        try:
            from Games.Custom.custom_game import download_missing_custom_game_images
            download_missing_custom_game_images(on_done=self._on_image_downloaded)
        except Exception:
            pass

