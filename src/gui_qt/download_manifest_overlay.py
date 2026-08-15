"""Collection manifest downloader - borderless in-window overlay (dev mode).

Paste one or more Nexus collection URLs (one per line); each collection's
``.7z`` manifest archive is downloaded to the Downloads folder. Reached from
Nexus ▸ Collections ▸ Download Manifest, which only appears in dev mode.

``on_accept(urls)`` is called with the non-empty lines on Download.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QWidget, QHBoxLayout, QLabel, QPushButton, QPlainTextEdit,
)

from gui_qt.overlay_base import OverlayBase
from gui_qt.theme_qt import active_palette, _c


class DownloadManifestOverlay(OverlayBase):
    CARD_W = 620
    CARD_H = 380
    MIN_W = 380
    MIN_H = 260

    def __init__(self, host: QWidget, dest_dir: str, on_accept, initial: str = ""):
        super().__init__(host)
        self._on_accept = on_accept
        p = active_palette()

        _card, v = self._make_card("_DownloadManifestCard")

        title_lbl = QLabel(self.tr("Download collection manifest"))
        title_lbl.setStyleSheet(
            f"color:{_c(p,'TEXT_MAIN')}; font-weight:600; font-size:15px;")
        v.addWidget(title_lbl)

        sub = QLabel(self.tr(
            "One collection URL per line, e.g. "
            "https://www.nexusmods.com/games/fallout4/collections/f1rzym"))
        sub.setWordWrap(True)
        sub.setStyleSheet(f"color:{_c(p,'TEXT_DIM')}; font-size:11px;")
        v.addWidget(sub)

        self._edit = QPlainTextEdit()
        self._edit.setPlainText(initial or "")
        self._edit.setLineWrapMode(QPlainTextEdit.NoWrap)
        self._edit.setStyleSheet(
            f"QPlainTextEdit {{ background:{_c(p,'BG_LIST')};"
            f" color:{_c(p,'TEXT_MAIN')};"
            f" border:1px solid {_c(p,'BORDER')}; border-radius:4px; }}")
        v.addWidget(self._edit, 1)

        dest = QLabel(self.tr("Saving to: {0}").format(dest_dir))
        dest.setWordWrap(True)
        dest.setStyleSheet(f"color:{_c(p,'TEXT_DIM')}; font-size:11px;")
        v.addWidget(dest)

        bar = QHBoxLayout()
        bar.addStretch(1)
        cancel = QPushButton(self.tr("Cancel"))
        cancel.setObjectName("FormButton")
        cancel.setCursor(Qt.PointingHandCursor)
        cancel.clicked.connect(lambda: self._finish())
        bar.addWidget(cancel)
        go = QPushButton(self.tr("Download"))
        go.setObjectName("PrimaryButton")
        go.setCursor(Qt.PointingHandCursor)
        go.clicked.connect(self._accept)
        bar.addWidget(go)
        v.addLayout(bar)

        self._present()
        self._edit.setFocus()

    @classmethod
    def show_over(cls, host, dest_dir, on_accept, initial=""):
        top = host.window() if host is not None else None
        return cls(top or host, dest_dir, on_accept, initial=initial)

    # -- internals ----------------------------------------------------------
    def _accept(self):
        if self._done:
            return
        urls = [ln.strip() for ln in self._edit.toPlainText().splitlines()
                if ln.strip()]
        self._finish()
        if self._on_accept is not None and urls:
            self._on_accept(urls)
