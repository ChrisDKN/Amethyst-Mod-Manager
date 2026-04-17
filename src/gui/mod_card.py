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
import tkinter.font as tkfont

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
from gui.tk_tooltip import TkTooltip

# Card dimensions (shared with browse)
# CARD_W / CARD_H are passed to CTkFrame (which applies its own widget scaling),
# so keep them as design-pixel values — do NOT wrap in scaled().
# For tk-level layout math (slot widths, canvas offsets) use scaled(CARD_W).
CARD_W = 280
CARD_H = 324
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


def _truncate_to_lines(text: str, font: tkfont.Font, max_width_px: int, max_lines: int) -> str:
    """Word-wrap `text` to fit `max_width_px`; if it exceeds `max_lines`, truncate
    the last kept line with an ellipsis. Mirrors tk's greedy wrap behaviour."""
    words = text.split()
    if not words:
        return text
    lines: list[str] = []
    cur = ""
    for w in words:
        candidate = w if not cur else f"{cur} {w}"
        if font.measure(candidate) <= max_width_px:
            cur = candidate
            continue
        if cur:
            lines.append(cur)
            if len(lines) >= max_lines:
                break
        # word alone may exceed width — hard-break it
        if font.measure(w) > max_width_px:
            buf = ""
            for ch in w:
                if font.measure(buf + ch) <= max_width_px:
                    buf += ch
                else:
                    lines.append(buf)
                    if len(lines) >= max_lines:
                        buf = ""
                        break
                    buf = ch
            cur = buf
        else:
            cur = w
    if cur and len(lines) < max_lines:
        lines.append(cur)

    # Check whether we consumed every word
    consumed = " ".join(lines).split()
    if len(consumed) >= len(words):
        return "\n".join(lines)

    # Need to ellipsise the last line
    last = lines[-1] if lines else ""
    ell = "…"
    while last and font.measure(last + ell) > max_width_px:
        last = last[:-1].rstrip()
    lines[-1] = (last + ell) if last else ell
    return "\n".join(lines)


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

        title_font = font_sized(FONT_FAMILY, 13, "bold")
        # Rebuild a tkfont.Font from the same tuple so text measurements match
        # the rendered label (size is already scaled inside font_sized()).
        title_measure_font = tkfont.Font(
            family=title_font[0], size=title_font[1], weight="bold",
        )
        # Font size is already scaled; measure() returns scaled pixels, so
        # compare against the scaled design width.
        title_text = _truncate_to_lines(
            title_text, title_measure_font, scaled(CARD_W - 20), max_lines=2,
        )
        # Always render 2 lines so every card reserves identical vertical
        # space for the title — buttons then line up across the grid.
        if "\n" not in title_text:
            title_text += "\n "
        # Reserve 2 lines of height so buttons line up across cards regardless
        # of whether the title wraps to 1 or 2 lines. CTkLabel height takes
        # design pixels (it scales internally), so derive from the unscaled
        # point size rather than measure() which returns scaled px.
        # Compute fixed 2-line height in actual pixels from the real font
        # metrics. Used for both the label height and the grid row minsize so
        # 1-line and 2-line titles occupy identical vertical space.
        title_two_line_px = title_measure_font.metrics("linespace") * 2 + scaled(4)
        title_label = ctk.CTkLabel(
            self.card, text=title_text,
            font=title_font,
            anchor="nw", justify="left",
        )
        title_label.grid(
            row=1, column=0, padx=10, pady=(6, 0),
            sticky="new", columnspan=2,
        )
        self.card.grid_rowconfigure(1, minsize=title_two_line_px)

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
        btn_frame.grid(row=5, column=0, padx=8, pady=(4, 14), sticky="sew", columnspan=2)
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
        self._tooltip = TkTooltip(
            self.card,
            bg=BG_DEEP, fg=TEXT_MAIN,
            font=font_sized(FONT_FAMILY, 11),
            wraplength=scaled(320), padx=scaled(8), pady=scaled(6),
            alpha=0.95,
        )
        self._tooltip.attach(self.card, text, offset_x=scaled(12), offset_y=scaled(12))

    def _apply_image(self, photo: ctk.CTkImage):
        if self._img_label.winfo_exists():
            self._img_label.configure(image=photo)
            self._image_loaded = True
