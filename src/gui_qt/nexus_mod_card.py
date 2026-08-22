"""A single Nexus mod card + an async thumbnail loader.

`NexusModCard` mirrors the nexusmods.com browse card: cover image, title, author,
category, "updated X ago / uploaded date", a multi-line description, and a bottom
stats bar (endorsements / downloads / size), with View / Install buttons. It uses
the `#GameCard` chrome so it matches the rest of the Qt UI. Thumbnails are fetched
off the UI thread by `ThumbnailLoader` and delivered back via a Qt signal (keyed by
mod_id), with a small in-memory LRU cache shared across the browser (mirrors the Tk
`IMG_CACHE_MAX`).

All displayed fields come from the single GraphQL list call (NexusModInfo) - no
extra per-card API request, so the card is rate-limit-free.
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from datetime import datetime, timezone

from PySide6.QtCore import Qt, QObject, Signal, QCoreApplication
from PySide6.QtGui import QPixmap, QImage, QFontMetrics, QTextLayout
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QFrame,
)

from gui_qt.theme_qt import active_palette, _c, contrast_text

CARD_W = 300
CARD_H = 392
IMG_W = CARD_W - 2  # leave the card's 1px border visible on both sides
IMG_H = 150
IMG_CACHE_MAX = 120


def _fmt_count(n: int) -> str:
    """1234567 → '1.2m', 34487 → '34.4k', 812 → '812'."""
    n = int(n or 0)
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}m"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return str(n)


def _fmt_size_kb(kb: int) -> str:
    """File size in KB → human string (KB/MB/GB)."""
    kb = int(kb or 0)
    if kb <= 0:
        return ""
    if kb >= 1024 * 1024:
        return f"{kb / (1024 * 1024):.1f}GB"
    if kb >= 1024:
        return f"{kb / 1024:.1f}MB"
    return f"{kb}KB"


def _parse_iso(s: str):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None


def cap_summary(text: str, limit: int = 180) -> str:
    """Truncate a card summary to ~*limit* chars at a word boundary and append an
    ellipsis (Nexus-style), so it never overflows the card's fixed-height box."""
    text = " ".join((text or "").split())          # collapse whitespace/newlines
    if len(text) <= limit:
        return text
    cut = text[:limit]
    sp = cut.rfind(" ")
    if sp > limit * 0.6:                            # prefer a word boundary
        cut = cut[:sp]
    return cut.rstrip(" ,.;:-") + "…"


def wrap_tooltip(text: str, width: int = 60) -> str:
    """Return a rich-text tooltip that word-wraps instead of running off one long
    line. A ``<qt width=...>`` tooltip makes Qt wrap at that pixel width."""
    from html import escape
    text = " ".join((text or "").split())
    return f"<qt width='{width * 6}'>{escape(text)}</qt>" if text else ""


def _ago(s: str) -> str:
    """ISO timestamp → '3 days ago' / '4 months ago' (matches the Nexus card)."""
    dt = _parse_iso(s)
    if dt is None:
        return ""
    now = datetime.now(timezone.utc)
    secs = max(0, (now - dt).total_seconds())
    mins = secs / 60
    hours = mins / 60
    days = hours / 24
    # Module-level fn (no QObject `self`), so translate via QCoreApplication.
    _t = lambda s: QCoreApplication.translate("nexus_mod_card", s)
    if days >= 365:
        v = int(days / 365)
        return (_t("{0} year ago") if v == 1 else _t("{0} years ago")).format(v)
    if days >= 30:
        v = int(days / 30)
        return (_t("{0} month ago") if v == 1 else _t("{0} months ago")).format(v)
    if days >= 1:
        v = int(days)
        return (_t("{0} day ago") if v == 1 else _t("{0} days ago")).format(v)
    if hours >= 1:
        v = int(hours)
        return (_t("{0} hour ago") if v == 1 else _t("{0} hours ago")).format(v)
    if mins >= 1:
        v = int(mins)
        return (_t("{0} minute ago") if v == 1 else _t("{0} minutes ago")).format(v)
    return _t("just now")


def _fmt_date(s: str) -> str:
    """ISO timestamp → '14 Sept 2018' style (uploaded date)."""
    dt = _parse_iso(s)
    if dt is None:
        return ""
    return dt.strftime("%d %b %Y").lstrip("0")


def _cover_scale(img: QImage, w: int, h: int) -> QImage:
    """Center-crop *img* to the w:h aspect ratio, then scale to (w, h).
    Works on QImage (not QPixmap) so it can run on the fetch worker -
    QPixmap is documented GUI-thread-only."""
    if img.isNull():
        return img
    sw, sh = img.width(), img.height()
    target = w / h
    src = sw / sh if sh else target
    if src > target:                     # too wide → crop sides
        new_w = int(sh * target)
        x = (sw - new_w) // 2
        img = img.copy(x, 0, new_w, sh)
    else:                                # too tall → crop top/bottom
        new_h = int(sw / target)
        y = (sh - new_h) // 2
        img = img.copy(0, y, sw, new_h)
    return img.scaled(w, h, Qt.IgnoreAspectRatio, Qt.SmoothTransformation)


class ThumbnailLoader(QObject):
    """Fetches thumbnails off the UI thread; emits `loaded(mod_id, QPixmap)`
    on the UI thread. Caches scaled pixmaps in an LRU dict (cover-cropped to the
    card image size). *crop_w*/*crop_h* set the target cover size - default is the
    mod card's inner width (298×150 landscape); the collections browser passes a
    portrait size."""

    loaded = Signal(int, object)         # (mod_id, QPixmap)
    # Fetch worker → GUI thread: the decoded + cropped QImage. The QPixmap
    # conversion happens in the slot - QPixmap is GUI-thread-only.
    _img_ready = Signal(int, str, object)

    def __init__(self, parent=None, crop_w: int = IMG_W, crop_h: int = IMG_H):
        super().__init__(parent)
        self._cache: "OrderedDict[str, QPixmap]" = OrderedDict()
        self._inflight: set[str] = set()
        self._lock = threading.Lock()
        self._crop_w = crop_w
        self._crop_h = crop_h
        self._img_ready.connect(self._on_img_ready)

    def cached(self, url: str) -> QPixmap | None:
        with self._lock:
            pm = self._cache.get(url)
            if pm is not None:
                self._cache.move_to_end(url)
            return pm

    def request(self, mod_id: int, url: str) -> None:
        if not url:
            return
        cached = self.cached(url)
        if cached is not None:
            self.loaded.emit(mod_id, cached)
            return
        with self._lock:
            if url in self._inflight:
                return
            self._inflight.add(url)
        threading.Thread(target=self._worker, args=(mod_id, url), daemon=True).start()

    def _worker(self, mod_id: int, url: str) -> None:
        img = QImage()
        try:
            import requests
            from Utils.ca_bundle import resolve_ca_bundle
            resp = requests.get(url, timeout=10, verify=resolve_ca_bundle() or True)
            if resp.ok:
                img = QImage.fromData(resp.content)
                if not img.isNull():
                    img = _cover_scale(img, self._crop_w, self._crop_h)
        except Exception:
            img = QImage()
        with self._lock:
            self._inflight.discard(url)
        if not img.isNull():
            try:
                self._img_ready.emit(mod_id, url, img)
            except RuntimeError:
                pass        # view/loader was destroyed while the fetch was in flight

    def _on_img_ready(self, mod_id: int, url: str, img: QImage) -> None:
        """GUI thread: convert the fetched QImage, cache, notify the cards."""
        pm = QPixmap.fromImage(img)
        if pm.isNull():
            return
        with self._lock:
            self._cache[url] = pm
            while len(self._cache) > IMG_CACHE_MAX:
                self._cache.popitem(last=False)
        self.loaded.emit(mod_id, pm)


class _TwoLineLabel(QLabel):
    """A label that wraps to at most 2 lines and elides the overflow with '…'
    (matching the nexusmods.com card title behaviour). Re-elides on resize."""

    def __init__(self, text: str, parent=None):
        super().__init__(parent)
        self._full = text
        self.setWordWrap(True)
        self.setTextFormat(Qt.PlainText)
        self.setAlignment(Qt.AlignLeft | Qt.AlignTop)
        self.setToolTip(text)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._relayout()

    def _relayout(self):
        fm = QFontMetrics(self.font())
        w = max(1, self.width())
        layout = QTextLayout(self._full, self.font())
        layout.beginLayout()
        lines = []
        while True:
            line = layout.createLine()
            if not line.isValid():
                break
            line.setLineWidth(w)
            lines.append(line)
        layout.endLayout()
        if len(lines) <= 2:
            super().setText(self._full)
            return
        # Keep text up to the start of the 3rd line, then elide that slice.
        cut = lines[2].textStart()
        head = self._full[:cut]
        elided = fm.elidedText(head, Qt.ElideRight, w)
        super().setText(elided)


class NexusModCard(QFrame):
    """One mod in the grid. *entry* is a NexusModInfo. Callbacks:
        on_view(entry)     - show the mod's details
        on_install(entry)  - download + install
        on_context(entry, global_pos) - show a right-click menu (optional)
    """

    def __init__(self, entry, on_view, on_install, on_context=None,
                 is_installed: bool = False, download_only: bool = False,
                 parent=None):
        super().__init__(parent)
        self.setObjectName("GameCard")
        self.setFixedSize(CARD_W, CARD_H)
        self.entry = entry
        self._on_context = on_context
        self._installed = bool(is_installed)
        self._watching = False
        self._download_only = bool(download_only)
        p = active_palette()
        self._pal = p
        dim = _c(p, "TEXT_DIM")

        v = QVBoxLayout(self)
        # QFrame's styled 1px border already insets the layout's content rect.
        # IMG_W matches that inner width so the cover cannot paint over it.
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)

        # --- cover image (full-bleed top) ----------------------------------
        self._img = QLabel()
        self._img.setAlignment(Qt.AlignCenter)
        self._img.setFixedSize(IMG_W, IMG_H)
        self._img.setStyleSheet(
            f"background:{_c(p,'BG_DEEP')};"
            f" border-top-left-radius:8px; border-top-right-radius:8px;"
            f" color:{dim};")
        self._img.setText("…")
        v.addWidget(self._img)

        # --- body ----------------------------------------------------------
        body = QWidget()
        bl = QVBoxLayout(body)
        bl.setContentsMargins(10, 8, 10, 8)
        bl.setSpacing(3)

        title = _TwoLineLabel(entry.name or f"Mod {entry.mod_id}")
        title.setObjectName("GameCardName")
        title.setStyleSheet(
            f"color:{_c(p,'TEXT_MAIN')}; font-weight:600; font-size:13px;")
        title.setFixedHeight(36)
        bl.addWidget(title)

        if entry.author:
            author = QLabel(self.tr("by {0}").format(entry.author))
            author.setStyleSheet(f"color:{dim}; font-size:11px;")
            author.setMaximumWidth(CARD_W - 20)
            bl.addWidget(author)

        if entry.category_name:
            cat = QLabel(entry.category_name)
            cat.setStyleSheet(f"color:{_c(p,'ACCENT')}; font-size:11px;")
            bl.addWidget(cat)

        # "updated X ago · uploaded date" (only the parts we have).
        ago = _ago(entry.updated_at)
        up = _fmt_date(entry.created_at)
        date_bits = []
        if ago:
            date_bits.append(f"⟳ {ago}")
        if up:
            date_bits.append(f"⬆ {up}")
        if date_bits:
            dates = QLabel("   ".join(date_bits))
            dates.setStyleSheet(f"color:{dim}; font-size:10px;")
            bl.addWidget(dates)

        # Description (the summary) - fills the remaining space, length-capped
        # (Nexus-style) with the full text word-wrapped in the tooltip.
        _summary = (entry.summary or "").strip()
        desc = QLabel(cap_summary(_summary))
        desc.setWordWrap(True)
        desc.setAlignment(Qt.AlignLeft | Qt.AlignTop)
        desc.setStyleSheet(f"color:{dim}; font-size:12px;")
        if _summary:
            desc.setToolTip(wrap_tooltip(_summary))
        desc.setSizePolicy(desc.sizePolicy().horizontalPolicy(),
                           desc.sizePolicy().Policy.Ignored)
        bl.addWidget(desc, 1)

        # Stats bar: ♥ endorsements · ↓ downloads · ▢ size.
        sbits = [f"♥ {_fmt_count(entry.endorsement_count)}",
                 f"↓ {_fmt_count(entry.downloads_total)}"]
        size = _fmt_size_kb(getattr(entry, "file_size_kb", 0))
        if size:
            sbits.append(f"▢ {size}")
        stats = QLabel("    ".join(sbits))
        stats.setStyleSheet(
            f"color:{_c(p,'TEXT_MAIN')}; font-size:11px;"
            f" border-top:1px solid {_c(p,'BORDER')}; padding-top:5px;")
        bl.addWidget(stats)

        # Buttons: View (blue) + Install (green).
        row = QHBoxLayout()
        row.setSpacing(6)
        view = QPushButton(self.tr("View"))
        view.setObjectName("GameAddBtn")          # blue accent
        view.setCursor(Qt.PointingHandCursor)
        view.clicked.connect(lambda: on_view(entry))
        row.addWidget(view, 1)
        self._install_btn = QPushButton()
        self._install_btn.setObjectName("GameSelectBtn")    # green base style
        self._install_btn.setCursor(Qt.PointingHandCursor)
        self._install_btn.clicked.connect(lambda: on_install(entry))
        row.addWidget(self._install_btn, 1)
        bl.addLayout(row)
        self._apply_install_style()

        v.addWidget(body, 1)

    def set_installed(self, installed: bool) -> None:
        if bool(installed) == self._installed:
            return
        self._installed = bool(installed)
        self._apply_install_style()

    def set_download_only(self, on: bool) -> None:
        """Install/Reinstall become Download/Redownload."""
        if bool(on) == self._download_only:
            return
        self._download_only = bool(on)
        self._apply_install_style()

    def set_watching(self, watching: bool) -> None:
        """A non-premium install is waiting for this mod's browser download -
        the Install button becomes a red Cancel (the click toggles the watch)."""
        if bool(watching) == self._watching:
            return
        self._watching = bool(watching)
        self._apply_install_style()

    def _apply_install_style(self):
        if self._watching:
            danger = _c(self._pal, "BTN_DANGER")
            self._install_btn.setText(self.tr("Cancel"))
            self._install_btn.setStyleSheet(
                f"QPushButton{{background:{danger}; color:{contrast_text(danger)}; font-weight:600;"
                f" border:none; border-radius:4px; padding:5px 0;}}"
                f"QPushButton:hover{{background:{danger};}}")
        elif self._installed:
            # Reinstall - orange (BTN_WARN), like the Downloads tab.
            warn = _c(self._pal, "BTN_WARN")
            self._install_btn.setText(self.tr("Redownload") if self._download_only
                                      else self.tr("Reinstall"))
            self._install_btn.setStyleSheet(
                f"QPushButton{{background:{warn}; color:{contrast_text(warn)}; font-weight:600;"
                f" border:none; border-radius:4px; padding:5px 0;}}"
                f"QPushButton:hover{{background:{warn};}}")
        else:
            # Install - clear the inline style so the green #GameSelectBtn QSS shows.
            self._install_btn.setText(self.tr("Download") if self._download_only
                                      else self.tr("Install"))
            self._install_btn.setStyleSheet("")

    def set_thumbnail(self, pm: QPixmap) -> None:
        if pm is not None and not pm.isNull():
            self._img.setText("")
            self._img.setPixmap(pm)

    def contextMenuEvent(self, event):
        if self._on_context is not None:
            self._on_context(self.entry, event.globalPos())
        else:
            super().contextMenuEvent(event)
