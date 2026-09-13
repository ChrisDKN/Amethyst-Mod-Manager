from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import QFrame, QLabel, QPushButton, QVBoxLayout, QHBoxLayout, QWidget

from gui_qt.icons import icon
from gui_qt.nexus_mod_card import _TwoLineLabel, cap_summary, wrap_tooltip
from gui_qt.theme_qt import active_palette, _c
from Utils.collections.manifest import fmt_size

CARD_W, CARD_H = 300, 448
IMG_W, IMG_H = CARD_W - 2, 168


class WabbajackCard(QFrame):
    activated = Signal()
    context_requested = Signal(object)

    def __init__(self, entry, info=None, *, update_available=False, parent=None):
        super().__init__(parent)
        self.entry, self.info = entry, info
        self.setObjectName("WabbajackCard")
        self.setFixedSize(CARD_W, CARD_H)
        palette = active_palette()
        dim = _c(palette, "TEXT_DIM")
        self.setStyleSheet(
            f"#WabbajackCard {{ background:{_c(palette, 'BG_PANEL')}; border:1px solid {_c(palette, 'BORDER')}; border-radius:8px; }}"
            f"#WabbajackCard:hover {{ border-color:{_c(palette, 'ACCENT')}; }}")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(1, 1, 1, 1)
        layout.setSpacing(0)
        self._image = QLabel(self)
        self._image.setAlignment(Qt.AlignCenter)
        self._image.setFixedSize(IMG_W, IMG_H)
        self._image.setStyleSheet(f"background:{_c(palette, 'BG_DEEP')}; border-radius:7px;")
        self._image.setPixmap(icon("Wabbajack.png", 80).pixmap(80, 80))
        layout.addWidget(self._image)
        body = QWidget(self)
        content = QVBoxLayout(body)
        content.setContentsMargins(10, 9, 10, 9)
        content.setSpacing(5)
        title = _TwoLineLabel(entry.title, body)
        title.setFixedHeight(36)
        title.setStyleSheet(f"color:{_c(palette, 'TEXT_MAIN')}; font-size:13px; font-weight:600;")
        content.addWidget(title)
        byline = self.tr("by {0}").format(entry.author or self.tr("Unknown author"))
        if entry.version:
            byline += " · " + entry.version
        author = self._line(byline, dim)
        content.addWidget(author)
        description = QLabel(cap_summary(entry.description, limit=110), body)
        description.setTextFormat(Qt.PlainText)
        description.setWordWrap(True)
        description.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        description.setFixedHeight(56)
        description.setStyleSheet(f"color:{dim}; font-size:12px;")
        description.setToolTip(wrap_tooltip(entry.description))
        content.addWidget(description)
        badges = []
        if entry.featured:
            badges.append(self.tr("Featured"))
        if entry.nsfw:
            badges.append(self.tr("Adult"))
        if entry.unavailable:
            badges.append(self.tr("Unavailable"))
        if info:
            status = self.tr("Resume available") if info.get("status") != "complete" else (
                self.tr("Update available") if update_available else self.tr("Installed"))
            badges.insert(0, status)
        content.addWidget(self._line(" · ".join(badges), _c(palette, "ACCENT")))
        content.addWidget(self._line(" · ".join(entry.tags[:3]), dim))
        sizes = QHBoxLayout()
        for label, value in ((self.tr("Download"), entry.download_size), (self.tr("Install"), entry.install_size)):
            stat = QLabel(label + "\n" + (fmt_size(value) if value else self.tr("Unknown")), body)
            stat.setStyleSheet(f"color:{_c(palette, 'TEXT_MAIN')}; font-size:11px; border-top:1px solid {_c(palette, 'BORDER')}; padding-top:5px;")
            sizes.addWidget(stat, 1)
        content.addLayout(sizes)
        self._view_button = QPushButton(self.tr("View"), body)
        self._view_button.setObjectName("GameAddBtn")
        self._view_button.setCursor(Qt.PointingHandCursor)
        self._view_button.clicked.connect(self.activated)
        content.addWidget(self._view_button)
        layout.addWidget(body, 1)
        if info:
            self.setToolTip(wrap_tooltip(self.tr("Installation: {0}").format(info.get("directory", info.get("id", "")))))

    def _line(self, text, color):
        label = QLabel(text, self)
        label.setTextFormat(Qt.PlainText)
        label.setFixedHeight(17)
        label.setMaximumWidth(CARD_W - 22)
        label.setToolTip(wrap_tooltip(text))
        label.setStyleSheet(f"color:{color}; font-size:11px;")
        label.ensurePolished()
        label.setText(label.fontMetrics().elidedText(text, Qt.ElideRight, CARD_W - 22))
        return label

    def set_thumbnail(self, pixmap: QPixmap):
        if pixmap is not None and not pixmap.isNull():
            self._image.setPixmap(pixmap)

    def contextMenuEvent(self, event):
        self.context_requested.emit(event.globalPos())
        event.accept()
