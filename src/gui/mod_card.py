"""
Shared ModCard component — used by Browse, Tracked, and Endorsed panels to
display mods as CTkCards with image, title, stats, author, summary, View/Install buttons.

Each panel supplies its own context menu via the on_right_click callback.
"""

from __future__ import annotations

import io
import threading
from typing import Callable, Any

import tkinter as tk

import customtkinter as ctk
import requests
from PIL import Image as PilImage

from gui.ctk_components import CTkCard
from gui.theme import (
    ACCENT,
    ACCENT_HOV,
    BG_DEEP,
    TEXT_DIM,
    TEXT_MAIN,
    font_sized,
    FONT_FAMILY,
    scaled,
)

# Card dimensions (shared with browse)
# CARD_W / CARD_H are passed to CTkFrame (which applies its own widget scaling),
# so keep them as design-pixel values — do NOT wrap in scaled().
# For tk-level layout math (slot widths, canvas offsets) use scaled(CARD_W).
CARD_W = 280
CARD_H = 310
# Image dimensions — unscaled design values. CTkImage/CTkLabel receive these
# directly; CTk applies set_widget_scaling internally (scaled() would double-scale).
CARD_IMG_W = CARD_W - 10
CARD_IMG_H = 160
CARD_PAD = scaled(10)
CARD_COLS = 2

PLACEHOLDER_COLOR = "#3a3a3a"

# Shared placeholder — created once, reused by all ModCards
_PLACEHOLDER_PIL: PilImage.Image | None = None
_PLACEHOLDER_CTK: ctk.CTkImage | None = None


def _get_placeholder() -> ctk.CTkImage:
    """Return a shared CTkImage placeholder (created on first call)."""
    global _PLACEHOLDER_PIL, _PLACEHOLDER_CTK
    if _PLACEHOLDER_CTK is None:
        _PLACEHOLDER_PIL = PilImage.new("RGB", (CARD_IMG_W, CARD_IMG_H), PLACEHOLDER_COLOR)
        _PLACEHOLDER_CTK = ctk.CTkImage(_PLACEHOLDER_PIL, _PLACEHOLDER_PIL, (CARD_IMG_W, CARD_IMG_H))
    return _PLACEHOLDER_CTK


def make_placeholder_image(w: int, h: int) -> PilImage.Image:
    """Create a solid-colour placeholder PIL image."""
    return PilImage.new("RGB", (w, h), PLACEHOLDER_COLOR)


class ModCard:
    """
    CTkCard wrapper for displaying a mod with image, title, stats, summary,
    and View/Install buttons.
    """

    def __init__(
        self,
        master: ctk.CTkFrame,
        entry: Any,
        on_view: Callable,
        on_install: Callable,
        on_right_click: Callable,
        is_installed: bool = False,
    ):
        self._entry = entry
        self._image_loaded = False

        def _get(e, k, d=""):
            if isinstance(e, dict):
                return e.get(k, d)
            return getattr(e, k, d)

        # Card frame
        self.card = CTkCard(master, width=CARD_W, height=CARD_H)
        self.card.grid_propagate(False)
        self.card.grid_columnconfigure(0, weight=1)

        # Image placeholder (shared across all cards)
        self._placeholder_ctk = _get_placeholder()
        self._img_label = ctk.CTkLabel(self.card, text="", image=self._placeholder_ctk)
        self._img_label.grid(row=0, column=0, padx=5, pady=(5, 0), sticky="ew", columnspan=2)

        # Title + stats
        title_text = _get(entry, "name") or f"Mod {_get(entry, 'mod_id', 0)}"
        stats_parts = []
        if _get(entry, "version"):
            stats_parts.append(f"v{_get(entry, 'version')}")
        if _get(entry, "downloads_total", 0):
            stats_parts.append(f"↓{_get(entry, 'downloads_total', 0):,}")
        if _get(entry, "endorsement_count", 0):
            stats_parts.append(f"♥{_get(entry, 'endorsement_count', 0):,}")
        stats_str = "  ".join(stats_parts)

        title_label = ctk.CTkLabel(
            self.card, text=title_text,
            font=font_sized(FONT_FAMILY, 13, "bold"),
            anchor="w", wraplength=CARD_W - 20, justify="left",
        )
        title_label.grid(row=1, column=0, padx=10, pady=(6, 0), sticky="ew", columnspan=2)

        if stats_str:
            stats_label = ctk.CTkLabel(
                self.card, text=stats_str,
                font=font_sized(FONT_FAMILY, 11),
                text_color=TEXT_DIM,
                anchor="w",
            )
            stats_label.grid(row=2, column=0, padx=10, pady=(0, 2), sticky="nw", columnspan=2)

        # Author
        author = _get(entry, "author")
        if author:
            author_label = ctk.CTkLabel(
                self.card, text=f"by {author}",
                font=font_sized(FONT_FAMILY, 11),
                text_color=TEXT_DIM,
                anchor="w",
            )
            author_label.grid(row=3, column=0, padx=10, pady=(0, 2), sticky="nw", columnspan=2)

        # Summary shown as hover tooltip instead of inline text.
        summary = (str(_get(entry, "summary", "") or "")).strip()
        if summary:
            self._attach_tooltip(summary)

        self.card.grid_rowconfigure(4, weight=1)

        # Buttons — each 50% of card width with padding between
        btn_frame = ctk.CTkFrame(self.card, fg_color="transparent")
        btn_frame.grid(row=5, column=0, padx=8, pady=(4, 8), sticky="swe", columnspan=2)
        btn_frame.grid_columnconfigure(0, weight=1)
        btn_frame.grid_columnconfigure(1, weight=1)

        view_btn = ctk.CTkButton(
            btn_frame, text="View",
            height=30, fg_color=ACCENT, hover_color=ACCENT_HOV,
            font=font_sized(FONT_FAMILY, 12), command=on_view,
        )
        view_btn.grid(row=0, column=0, padx=(0, 4), sticky="ew")

        install_btn = ctk.CTkButton(
            btn_frame, text="Reinstall" if is_installed else "Install",
            height=30,
            fg_color="#c37800" if is_installed else "#2d7a2d",
            hover_color="#e28b00" if is_installed else "#3a9e3a",
            font=font_sized(FONT_FAMILY, 12), command=on_install,
        )
        install_btn.grid(row=0, column=1, padx=(4, 0), sticky="ew")

        for widget in (self.card, self._img_label, title_label, btn_frame):
            widget.bind("<ButtonRelease-3>", on_right_click)

    def load_image_async(self, url: str, cache: dict, loading: set, parent, on_done: Callable | None = None):
        """Start async image load; update label when done. Calls on_done() (on main thread) when finished."""
        if not url or self._image_loaded:
            return
        if url in cache:
            self._apply_image(cache[url])
            return
        if url in loading:
            return
        loading.add(url)

        def _worker():
            photo = None
            try:
                resp = requests.get(url, timeout=10)
                resp.raise_for_status()
                img = PilImage.open(io.BytesIO(resp.content)).convert("RGB")
                resample = PilImage.Resampling.LANCZOS if hasattr(PilImage, "Resampling") else PilImage.LANCZOS  # type: ignore
                # Scale to cover the slot, then center-crop (no distortion, no letterbox).
                # PIL works in actual pixels so use scaled dims; CTkImage receives
                # unscaled design dims — CTk applies set_widget_scaling internally.
                iw, ih = scaled(CARD_IMG_W), scaled(CARD_IMG_H)
                src_w, src_h = img.size
                scale = max(iw / src_w, ih / src_h)
                new_w, new_h = int(src_w * scale), int(src_h * scale)
                img = img.resize((new_w, new_h), resample)
                x_off = (new_w - iw) // 2
                y_off = (new_h - ih) // 2
                img = img.crop((x_off, y_off, x_off + iw, y_off + ih))
                photo = ctk.CTkImage(img, img, (CARD_IMG_W, CARD_IMG_H))
            except Exception:
                photo = None

            def _done():
                loading.discard(url)
                if photo is not None:
                    cache[url] = photo
                    self._apply_image(photo)
                if on_done is not None:
                    on_done()

            parent.after(0, _done)

        threading.Thread(target=_worker, daemon=True).start()

    def _attach_tooltip(self, text: str) -> None:
        """Attach a hover tooltip showing the mod summary to the card."""
        self._tooltip_win: tk.Toplevel | None = None

        def _enter(event):
            if self._tooltip_win is not None:
                return
            tw = tk.Toplevel(self.card)
            tw.withdraw()
            tw.overrideredirect(True)
            tw.attributes("-alpha", 0.95)
            tw.configure(bg=BG_DEEP)
            tk.Label(
                tw, text=text,
                bg=BG_DEEP, fg=TEXT_MAIN,
                font=font_sized(FONT_FAMILY, 11),
                wraplength=scaled(320), justify="left",
                padx=scaled(8), pady=scaled(6),
            ).pack()
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

        def _bind_recursive(w, depth=0) -> None:
            w.bind("<Enter>", _enter, add="+")
            w.bind("<Leave>", _leave, add="+")
            if depth < 3:
                for child in w.winfo_children():
                    _bind_recursive(child, depth + 1)

        _bind_recursive(self.card)

    def _apply_image(self, photo: ctk.CTkImage):
        if self._img_label.winfo_exists():
            self._img_label.configure(image=photo)
            self._image_loaded = True
