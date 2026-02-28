"""
Shared ModCard component — used by Browse, Tracked, and Endorsed panels to
display mods as CTkCards with image, title, stats, author, summary, View/Install buttons.

Each panel supplies its own context menu via the on_right_click callback.
"""

from __future__ import annotations

import io
import threading
from typing import Callable, Any

import customtkinter as ctk
import requests
from PIL import Image as PilImage

from gui.ctk_components import CTkCard
from gui.theme import (
    ACCENT,
    ACCENT_HOV,
    TEXT_DIM,
)

# Card dimensions (shared with browse)
CARD_W = 280
CARD_H = 380
CARD_IMG_W = CARD_W - 10
CARD_IMG_H = 160
CARD_PAD = 10
CARD_COLS = 2

PLACEHOLDER_COLOR = "#3a3a3a"


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

        # Image placeholder
        placeholder = make_placeholder_image(CARD_IMG_W, CARD_IMG_H)
        self._placeholder_ctk = ctk.CTkImage(placeholder, placeholder, (CARD_IMG_W, CARD_IMG_H))
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
            font=("Segoe UI", 13, "bold"),
            anchor="w", wraplength=CARD_W - 20, justify="left",
        )
        title_label.grid(row=1, column=0, padx=10, pady=(6, 0), sticky="ew", columnspan=2)

        if stats_str:
            stats_label = ctk.CTkLabel(
                self.card, text=stats_str,
                font=("Segoe UI", 11),
                text_color=TEXT_DIM,
                anchor="w",
            )
            stats_label.grid(row=2, column=0, padx=10, pady=(0, 2), sticky="nw", columnspan=2)

        # Author
        author = _get(entry, "author")
        if author:
            author_label = ctk.CTkLabel(
                self.card, text=f"by {author}",
                font=("Segoe UI", 11),
                text_color=TEXT_DIM,
                anchor="w",
            )
            author_label.grid(row=3, column=0, padx=10, pady=(0, 2), sticky="nw", columnspan=2)

        # Summary
        summary = (str(_get(entry, "summary", "") or "")).strip()
        if len(summary) > 120:
            summary = summary[:117] + "…"
        if summary:
            summary_label = ctk.CTkLabel(
                self.card, text=summary,
                font=("Segoe UI", 11),
                text_color=TEXT_DIM,
                wraplength=CARD_W - 20,
                justify="left", anchor="nw",
            )
            summary_label.grid(row=4, column=0, padx=10, pady=(2, 4), sticky="nw", columnspan=2)

        self.card.grid_rowconfigure(5, weight=1)

        # Buttons
        btn_frame = ctk.CTkFrame(self.card, fg_color="transparent")
        btn_frame.grid(row=6, column=0, padx=8, pady=(4, 8), sticky="sw", columnspan=2)

        view_btn = ctk.CTkButton(
            btn_frame, text="View",
            width=80, height=30,
            fg_color=ACCENT, hover_color=ACCENT_HOV,
            font=("Segoe UI", 12),
            command=on_view,
        )
        view_btn.grid(row=0, column=0, padx=(0, 6))

        install_btn = ctk.CTkButton(
            btn_frame, text="Install",
            width=90, height=30,
            fg_color="#2d7a2d", hover_color="#3a9e3a",
            font=("Segoe UI", 12),
            command=on_install,
        )
        install_btn.grid(row=0, column=1)

        for widget in (self.card, self._img_label, title_label, btn_frame):
            widget.bind("<ButtonRelease-3>", on_right_click)

    def load_image_async(self, url: str, cache: dict, loading: set, parent):
        """Start async image load; update label when done."""
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
                img = img.resize((CARD_IMG_W, CARD_IMG_H), resample)
                photo = ctk.CTkImage(img, img, (CARD_IMG_W, CARD_IMG_H))
            except Exception:
                photo = None

            def _done():
                loading.discard(url)
                if photo is not None:
                    cache[url] = photo
                    self._apply_image(photo)

            parent.after(0, _done)

        threading.Thread(target=_worker, daemon=True).start()

    def _apply_image(self, photo: ctk.CTkImage):
        if self._img_label.winfo_exists():
            self._img_label.configure(image=photo)
            self._image_loaded = True
