"""ImageView — a full-size image viewer that opens as a tab (lightbox).

Used by the FOMOD wizard when an option image is clicked, and by the Saves
tab when a save's screenshot is clicked. Scrollwheel zooms (anchored under the
cursor), left-drag pans, double-click resets to fit. Reuses the Mod Files
preview canvas so behaviour stays consistent across the app.

Takes either a path to load or an already-decoded pixmap — a save screenshot
is raw pixels out of the save file with no image on disk to point at.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import QWidget, QVBoxLayout

from gui_qt.image_preview import _ImageCanvas, _load_qimage


class ImageView(QWidget):
    def __init__(self, image_path: "Path | None" = None, parent=None, *,
                 pixmap: "QPixmap | None" = None):
        super().__init__(parent)
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        self._canvas = _ImageCanvas()
        self._canvas.setToolTip(
            self.tr("Scroll to zoom · drag to pan · double-click to fit"))
        v.addWidget(self._canvas)

        if pixmap is not None:
            self._canvas.set_image(pixmap)
            return
        qi = _load_qimage(Path(image_path)) if image_path is not None else None
        self._canvas.set_image(QPixmap.fromImage(qi) if qi is not None else None)
