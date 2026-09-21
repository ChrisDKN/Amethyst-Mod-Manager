from __future__ import annotations

import threading

from PySide6.QtCore import Qt, Signal, QEvent
from PySide6.QtWidgets import (
    QWidget, QFrame, QLabel, QPushButton, QVBoxLayout, QHBoxLayout,
    QGridLayout, QScrollArea, QMenu,
)

from gui_qt.icons import icon
from gui_qt.nexus_mod_card import ThumbnailLoader, _TwoLineLabel, wrap_tooltip
from gui_qt.safe_emit import safe_emit
from gui_qt.theme_qt import active_palette, _c, close_button, contrast_text

CARD_W, CARD_H = 300, 382
IMG_W, IMG_H = CARD_W - 2, 140


class InstalledCollectionCard(QFrame):
    view_requested = Signal(object)
    remove_requested = Signal(object)
    toggle_requested = Signal(object)

    def __init__(self, installation, parent=None):
        super().__init__(parent)
        self.installation = installation
        self.setObjectName("InstalledCollectionCard")
        self.setFixedSize(CARD_W, CARD_H)
        palette = active_palette()
        self.setStyleSheet(
            f"#InstalledCollectionCard {{ background:{_c(palette, 'BG_PANEL')};"
            f" border:1px solid {_c(palette, 'BORDER')}; border-radius:8px; }}"
            f"#InstalledCollectionCard:hover {{ border-color:{_c(palette, 'ACCENT')}; }}"
            f"#RemoveCollectionButton {{ background:{_c(palette, 'BTN_DANGER')};"
            f" color:{contrast_text(_c(palette, 'BTN_DANGER'))}; border:none;"
            f" border-radius:4px; padding:6px 12px; font-weight:600; }}"
            f"#RemoveCollectionButton:hover {{ background:{_c(palette, 'BTN_DANGER_HOV')}; }}")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(1, 1, 1, 1)
        layout.setSpacing(0)
        self._image = QLabel(self)
        self._image.setAlignment(Qt.AlignCenter)
        self._image.setFixedSize(IMG_W, IMG_H)
        self._image.setPixmap(icon("nexus.png", 72).pixmap(72, 72))
        layout.addWidget(self._image)
        body = QWidget(self)
        content = QVBoxLayout(body)
        content.setContentsMargins(10, 8, 10, 9)
        content.setSpacing(5)
        title = _TwoLineLabel(installation.title, body)
        title.setFixedHeight(36)
        title.setStyleSheet(f"color:{_c(palette, 'TEXT_MAIN')}; font-size:13px; font-weight:600;")
        content.addWidget(title)
        record = installation.record
        revision = record.get("revision")
        if record.get("kind") == "host":
            lines = [installation.game_name,
                     self.tr("Profile: {0}").format(installation.profile_dir.name),
                     self.tr("Profile with appended collections")]
        else:
            lines = [
                self.tr("Revision: {0}").format(revision if revision is not None else self.tr("Unknown")),
                installation.game_name,
                self.tr("Profile: {0}").format(installation.profile_dir.name),
                self.tr("Collection profile"),
                self._status_text(record.get("status", "unknown")),
            ]
        for text in lines:
            line = QLabel(body)
            line.setTextFormat(Qt.PlainText)
            line.setStyleSheet(f"color:{_c(palette, 'TEXT_DIM')}; font-size:11px;")
            line.setFixedHeight(17)
            line.setToolTip(wrap_tooltip(text))
            line.setText(line.fontMetrics().elidedText(text, Qt.ElideRight, CARD_W - 22))
            content.addWidget(line)
        content.addStretch(1)
        if installation.appended_collections:
            dropdown = QPushButton(self.tr("Appended Collections ({0})").format(
                len(installation.appended_collections)), body)
            dropdown.setObjectName("GameAddBtn")
            dropdown.setCursor(Qt.PointingHandCursor)
            menu = QMenu(dropdown)
            for appended in installation.appended_collections:
                child = QMenu(self.tr("{0} · Revision: {1}").format(
                    appended.title, appended.record.get("revision")
                    if appended.record.get("revision") is not None else self.tr("Unknown")), menu)
                menu.addMenu(child)
                child.setToolTipsVisible(True)
                child.setToolTip(wrap_tooltip(self.tr("{0} · {1}").format(
                    appended.profile_dir.name,
                    self._status_text(appended.record.get("status", "unknown")))))
                toggle = child.addAction(self.tr("Enable/Disable mods"))
                toggle.setEnabled(not appended.locked)
                toggle.triggered.connect(lambda _checked=False, item=appended:
                                         self.toggle_requested.emit(item))
                remove_child = child.addAction(self.tr("Remove"))
                remove_child.setEnabled(not appended.locked)
                remove_child.triggered.connect(lambda _checked=False, item=appended:
                                               self.remove_requested.emit(item))
            dropdown.setMenu(menu)
            content.addWidget(dropdown)
        if record.get("kind") != "host":
            actions = QHBoxLayout()
            view = QPushButton(self.tr("View"), body)
            view.setObjectName("GameAddBtn")
            view.setCursor(Qt.PointingHandCursor)
            view.clicked.connect(lambda: self.view_requested.emit(installation))
            actions.addWidget(view, 1)
            remove = QPushButton(self.tr("Remove"), body)
            remove.setObjectName("RemoveCollectionButton")
            remove.setCursor(Qt.PointingHandCursor)
            remove.setEnabled(not installation.locked)
            if installation.locked:
                remove.setToolTip(self.tr("Unlock this profile before removing the collection."))
            remove.clicked.connect(lambda: self.remove_requested.emit(installation))
            actions.addWidget(remove, 1)
            content.addLayout(actions)
        layout.addWidget(body, 1)
        self.setToolTip(wrap_tooltip(str(installation.profile_dir)))

    def _status_text(self, value):
        return {
            "complete": self.tr("Installed"),
            "paused": self.tr("Paused"),
            "cancelled": self.tr("Cancelled · Partial installation"),
            "installing": self.tr("Incomplete"),
            "incomplete": self.tr("Incomplete"),
            "unknown": self.tr("Installation status unknown"),
        }.get(value, self.tr("Installation status unknown"))

    def set_thumbnail(self, pixmap):
        if pixmap is not None and not pixmap.isNull():
            self._image.setPixmap(pixmap)


class InstalledCollectionsView(QWidget):
    close_requested = Signal()
    _loaded = Signal(int, object, str)
    view_requested = Signal(object)
    remove_requested = Signal(object)
    toggle_requested = Signal(object)

    def __init__(self, *, log_fn=None, parent=None):
        super().__init__(parent)
        self._log = log_fn or (lambda _m: None)
        self._token = 0
        self._cards = []
        self._columns = 0
        self._thumb_sequence = 0
        self._thumb_cards = {}
        self._loaded.connect(self._on_loaded)
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        bar = QWidget(self)
        bar.setObjectName("HeaderBar")
        header = QHBoxLayout(bar)
        header.setContentsMargins(12, 8, 12, 8)
        header.addWidget(QLabel(self.tr("Installed Collections"), bar))
        header.addStretch(1)
        self._count = QLabel(bar)
        header.addWidget(self._count)
        refresh = QPushButton(self.tr("Refresh"), bar)
        refresh.setObjectName("FormButton")
        refresh.clicked.connect(self.refresh)
        header.addWidget(refresh)
        close = close_button(self.tr("✕ Close"))
        close.clicked.connect(self._close)
        header.addWidget(close)
        root.addWidget(bar)
        self._scroll = QScrollArea(self)
        self._scroll.setFrameShape(QFrame.NoFrame)
        self._scroll.setWidgetResizable(True)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        host = QWidget(self._scroll)
        self._grid = QGridLayout(host)
        self._grid.setContentsMargins(16, 12, 16, 12)
        self._grid.setSpacing(12)
        self._grid.setAlignment(Qt.AlignTop | Qt.AlignHCenter)
        self._scroll.setWidget(host)
        self._scroll.viewport().installEventFilter(self)
        root.addWidget(self._scroll, 1)
        self._status = QLabel(self.tr("Loading installed collections…"), self)
        self._status.setWordWrap(True)
        self._status.setMargin(8)
        self._status.setTextFormat(Qt.PlainText)
        root.addWidget(self._status)
        self._thumbs = ThumbnailLoader(self, crop_w=IMG_W, crop_h=IMG_H, fit=True)
        self._thumbs.loaded.connect(self._thumbnail)
        self.refresh()

    def refresh(self):
        self._token += 1
        token = self._token
        def worker():
            from Utils.collections.installed import installed_collections
            try:
                result = installed_collections()
                safe_emit(self._loaded, token, result, "")
            except Exception as exc:
                safe_emit(self._loaded, token, [], str(exc))
        threading.Thread(target=worker, daemon=True, name="installed-collections-scan").start()

    def _on_loaded(self, token, records, error):
        if token != self._token:
            return
        if error:
            self._status.setText(self.tr("Could not scan installed collections: {0}").format(error))
            self._log(error)
            return
        while self._grid.count():
            widget = self._grid.takeAt(0).widget()
            if widget is not None:
                widget.hide()
                widget.deleteLater()
        self._cards = []
        self._thumb_cards.clear()
        for installation in records:
            card = InstalledCollectionCard(installation, self._scroll.widget())
            card.view_requested.connect(self.view_requested)
            card.remove_requested.connect(self.remove_requested)
            card.toggle_requested.connect(self.toggle_requested)
            self._cards.append(card)
            info = installation.record.get("card") or {}
            url = info.get("tile_image_url") or info.get("image_url") or info.get("thumbnail_url")
            if url:
                self._thumb_sequence += 1
                key = self._thumb_sequence
                self._thumb_cards[key] = card
                self._thumbs.request(key, url)
        self._count.setText(self.tr("{0} collections").format(sum(
            (0 if item.record.get("kind") == "host" else 1)
            + len(item.appended_collections) for item in records)))
        self._status.setText(self.tr("No installed collections found.") if not records else "")
        self._layout_cards()

    def _layout_cards(self):
        self._columns = max(1, (self._scroll.viewport().width() - 32 + 12) // (CARD_W + 12))
        for index, card in enumerate(self._cards):
            self._grid.addWidget(card, index // self._columns, index % self._columns)

    def _thumbnail(self, key, pixmap):
        card = self._thumb_cards.get(key)
        if card is not None:
            card.set_thumbnail(pixmap)

    def eventFilter(self, obj, event):
        if obj is self._scroll.viewport() and event.type() == QEvent.Resize:
            self._layout_cards()
        return super().eventFilter(obj, event)

    def _close(self):
        self.close_requested.emit()
