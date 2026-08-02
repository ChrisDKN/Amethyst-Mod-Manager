"""Panel-scoped image preview widget for the Mod Files tab.

Loads via Pillow (so .dds/.tga/.tiff decode — QPixmap can't) and shows the image
fit-to-panel over a checkerboard backdrop (transparency visible). Scrollwheel
zooms (anchored under the cursor), left-drag pans, double-click resets to fit.
Used as a modlist-panel-scoped tab: the Mod Files tree stays live in the plugins
panel while the preview occupies the modlist region.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, QPointF
from PySide6.QtGui import QImage, QPixmap, QPainter, QColor, QBrush
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QLabel, QSizePolicy,
)

from gui_qt.theme_qt import active_palette, _c

PREVIEW_EXTS = {
    ".png", ".jpg", ".jpeg", ".bmp", ".gif", ".webp",
    ".tga", ".tif", ".tiff", ".ico", ".dds",
}


def _load_qimage(path: Path) -> QImage | None:
    """Load *path* to a QImage. Tries QImage first (fast for common formats),
    falls back to Pillow for .dds/.tga/etc."""
    img = QImage(str(path))
    if not img.isNull():
        return img
    try:
        import io
        from PIL import Image as PilImage
        from Utils.dds_compat import sanitise_dds
        # Through bytes so sRGB DXGI formats Pillow lacks can be remapped.
        with PilImage.open(io.BytesIO(sanitise_dds(path.read_bytes()))) as im:
            im = im.convert("RGBA")
            data = im.tobytes("raw", "RGBA")
            qi = QImage(data, im.width, im.height, QImage.Format_RGBA8888)
            return qi.copy()   # detach from the freed buffer
    except Exception:
        return None


class _ImageCanvas(QLabel):
    """Paints the image over a checkerboard with free zoom (scrollwheel) and
    pan (left-drag). Zoom is anchored under the cursor; double-click resets to
    fit-to-window."""

    _MIN_SCALE = 0.05
    _MAX_SCALE = 40.0
    _ZOOM_STEP = 1.15  # per wheel notch

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAlignment(Qt.AlignCenter)
        self.setMinimumSize(1, 1)
        self.setMouseTracking(True)
        self._pm: QPixmap | None = None
        self._scale = 1.0        # user-applied zoom on top of fit scale
        self._fit_scale = 1.0    # scale that makes the image fit the widget
        self._offset = QPointF(0.0, 0.0)  # top-left of image in widget coords
        self._fitting = True     # follow the fit scale until the user zooms/pans
        self._drag_from: QPointF | None = None
        self._drag_offset0 = QPointF(0.0, 0.0)
        self.setCursor(Qt.OpenHandCursor)

    def set_image(self, pm: QPixmap | None):
        self._pm = pm
        self._fitting = True
        self._recompute_fit()
        self.update()

    # -- geometry helpers ---------------------------------------------------
    def _recompute_fit(self):
        """Compute the fit scale and, while in fitting mode, centre the image."""
        if self._pm is None or self._pm.isNull():
            return
        w, h = self._pm.width(), self._pm.height()
        if w <= 0 or h <= 0:
            return
        self._fit_scale = min(self.width() / w, self.height() / h)
        if self._fitting:
            self._scale = 1.0
            self._center()

    def _eff_scale(self) -> float:
        return self._fit_scale * self._scale

    def _center(self):
        if self._pm is None or self._pm.isNull():
            return
        s = self._eff_scale()
        iw, ih = self._pm.width() * s, self._pm.height() * s
        self._offset = QPointF((self.width() - iw) / 2.0,
                               (self.height() - ih) / 2.0)

    def _clamp_offset(self):
        """Keep the image sensibly placed: centre it on each axis when it is
        smaller than the viewport, otherwise stop it leaving empty gaps."""
        if self._pm is None or self._pm.isNull():
            return
        s = self._eff_scale()
        iw, ih = self._pm.width() * s, self._pm.height() * s
        ox, oy = self._offset.x(), self._offset.y()
        if iw <= self.width():
            ox = (self.width() - iw) / 2.0
        else:
            ox = min(0.0, max(self.width() - iw, ox))
        if ih <= self.height():
            oy = (self.height() - ih) / 2.0
        else:
            oy = min(0.0, max(self.height() - ih, oy))
        self._offset = QPointF(ox, oy)

    # -- interaction --------------------------------------------------------
    def wheelEvent(self, e):
        if self._pm is None or self._pm.isNull():
            return
        delta = e.angleDelta().y()
        if delta == 0:
            return
        factor = self._ZOOM_STEP ** (delta / 120.0)
        old_eff = self._eff_scale()
        new_eff = max(self._MIN_SCALE, min(self._MAX_SCALE, old_eff * factor))
        if new_eff == old_eff:
            return
        # Anchor the zoom under the cursor: keep the image point beneath the
        # pointer fixed on screen.
        cursor = e.position()
        img_pt = (cursor - self._offset) / old_eff
        self._fitting = False
        self._scale = new_eff / self._fit_scale
        self._offset = cursor - img_pt * self._eff_scale()
        self._clamp_offset()
        self.update()
        e.accept()

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton and self._pm is not None:
            self._drag_from = e.position()
            self._drag_offset0 = QPointF(self._offset)
            self.setCursor(Qt.ClosedHandCursor)

    def mouseMoveEvent(self, e):
        if self._drag_from is not None:
            self._fitting = False
            self._offset = self._drag_offset0 + (e.position() - self._drag_from)
            self._clamp_offset()
            self.update()

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.LeftButton and self._drag_from is not None:
            self._drag_from = None
            self.setCursor(Qt.OpenHandCursor)

    def mouseDoubleClickEvent(self, e):
        self._fitting = True
        self._recompute_fit()
        self.update()

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._recompute_fit()
        if not self._fitting:
            self._clamp_offset()

    def paintEvent(self, _e):
        p = QPainter(self)
        self._paint_checker(p)
        if self._pm is None or self._pm.isNull():
            p.setPen(QColor("#aaa"))
            p.drawText(self.rect(), Qt.AlignCenter, "Image could not be loaded")
            p.end()
            return
        s = self._eff_scale()
        iw, ih = self._pm.width() * s, self._pm.height() * s
        p.setRenderHint(QPainter.SmoothPixmapTransform, True)
        p.drawPixmap(int(round(self._offset.x())), int(round(self._offset.y())),
                     int(round(iw)), int(round(ih)), self._pm)
        p.end()

    def _paint_checker(self, p: QPainter):
        tile = 12
        c1, c2 = QColor("#353535"), QColor("#454545")
        p.fillRect(self.rect(), QBrush(c1))
        for y in range(0, self.height(), tile):
            for x in range(0, self.width(), tile):
                if ((x // tile) + (y // tile)) % 2:
                    p.fillRect(x, y, tile, tile, c2)


class ImagePreview(QWidget):
    """A panel-scoped image preview: header (file name) + canvas. Fit by default;
    scrollwheel zooms (anchored under the cursor), left-drag pans, double-click
    resets to fit."""

    def __init__(self, path: Path, display_name: str = "", parent=None):
        super().__init__(parent)
        self.setObjectName("ImagePreview")
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)

        pal = active_palette()
        header = QLabel(display_name or path.name)
        header.setObjectName("ImagePreviewHeader")
        header.setStyleSheet(
            f"background:{_c(pal, 'BG_HEADER')}; color:{_c(pal, 'TEXT_MAIN')};"
            " padding:6px 10px; font-weight:600;")
        header.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        header.setToolTip(self.tr("Scroll to zoom · drag to pan · double-click to fit"))
        v.addWidget(header)

        self._canvas = _ImageCanvas()
        self._canvas.setStyleSheet(f"background:{_c(pal, 'BG_DEEP')};")
        v.addWidget(self._canvas, 1)

        qi = _load_qimage(path)
        self._canvas.set_image(QPixmap.fromImage(qi) if qi is not None else None)

    def set_image(self, path: Path, display_name: str = ""):
        """Swap the previewed image in place (browsing between files)."""
        qi = _load_qimage(path)
        self._canvas.set_image(QPixmap.fromImage(qi) if qi is not None else None)
