from __future__ import annotations

import threading
import time
from collections import OrderedDict, deque

from PySide6.QtCore import Qt, QObject, Signal
from PySide6.QtGui import QPixmap, QImage, QFontMetrics, QTextLayout
from PySide6.QtWidgets import QLabel

from Utils.diagnostics import performance as perftrace
from gui_qt.widgets.tooltips import escaped_tooltip

CARD_W = 300
CARD_H = 392
IMG_W = CARD_W - 2  # leave the card's 1px border visible on both sides
IMG_H = 150
IMG_CACHE_MAX = 120
IMG_WORKERS_MAX = 8


def _fmt_count(n: int) -> str:
    """1234567 → '1.2m', 34487 → '34.4k', 812 → '812'."""
    n = int(n or 0)
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}m"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return str(n)


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
    """Return card text hard-wrapped to a readable tooltip width."""
    text = " ".join((text or "").split())
    return escaped_tooltip(text, width)


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
    card image size). *crop_w*/*crop_h* set the target size. ``fit=True`` keeps
    the whole image inside those bounds instead of cover-cropping it."""

    loaded = Signal(int, object)         # (mod_id, QPixmap)
    # Fetch worker → GUI thread: the decoded + cropped QImage. The QPixmap
    # conversion happens in the slot - QPixmap is GUI-thread-only.
    _img_ready = Signal(int, str, object)

    def __init__(self, parent=None, crop_w: int = IMG_W, crop_h: int = IMG_H,
                 fit: bool = False):
        super().__init__(parent)
        self._cache: "OrderedDict[str, QPixmap]" = OrderedDict()
        self._inflight: set[str] = set()
        self._pending: deque[tuple[int, str]] = deque()
        self._workers = 0
        self._worker_state = {"closed": False}
        self._lock = threading.Lock()
        self._crop_w = crop_w
        self._crop_h = crop_h
        self._fit = fit
        self._img_ready.connect(self._on_img_ready)
        def stop(*_, pending=self._pending, lock=self._lock,
                 state=self._worker_state):
            with lock:
                state["closed"] = True
                pending.clear()
        self.destroyed.connect(stop)

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
            if self._worker_state["closed"] or url in self._inflight:
                return
            self._inflight.add(url)
            self._pending.append((mod_id, url))
            if self._workers >= IMG_WORKERS_MAX:
                return
            self._workers += 1
        threading.Thread(target=self._drain, daemon=True,
                         name="nexus-thumbnail").start()

    def discard_pending(self) -> None:
        with self._lock:
            for _mod_id, url in self._pending:
                self._inflight.discard(url)
            self._pending.clear()

    def _drain(self) -> None:
        while True:
            with self._lock:
                if self._worker_state["closed"] or not self._pending:
                    self._workers -= 1
                    return
                mod_id, url = self._pending.popleft()
            self._worker(mod_id, url)

    def _worker(self, mod_id: int, url: str) -> None:
        img = QImage()
        tracing = perftrace.is_enabled()
        try:
            import requests
            from Utils.ca_bundle import resolve_ca_bundle
            started = time.perf_counter() if tracing else 0.0
            try:
                resp = requests.get(url, timeout=10, verify=resolve_ca_bundle() or True)
            finally:
                if tracing:
                    perftrace.record("nexus.thumbnail.http", time.perf_counter() - started)
            if resp.ok:
                started = time.perf_counter() if tracing else 0.0
                img = QImage.fromData(resp.content)
                if not img.isNull():
                    if self._fit:
                        img = img.scaled(
                            self._crop_w, self._crop_h, Qt.KeepAspectRatio,
                            Qt.SmoothTransformation)
                    else:
                        img = _cover_scale(img, self._crop_w, self._crop_h)
                if tracing:
                    perftrace.record("nexus.thumbnail.decode_scale", time.perf_counter() - started)
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
        started = time.perf_counter() if perftrace.is_enabled() else 0.0
        pm = QPixmap.fromImage(img)
        if pm.isNull():
            return
        with self._lock:
            self._cache[url] = pm
            while len(self._cache) > IMG_CACHE_MAX:
                self._cache.popitem(last=False)
        self.loaded.emit(mod_id, pm)
        if started:
            perftrace.record("nexus.thumbnail.apply", time.perf_counter() - started)


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
