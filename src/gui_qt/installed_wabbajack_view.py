from __future__ import annotations

import threading

from PySide6.QtCore import Qt, Signal, QEvent
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QFrame, QGridLayout, QHBoxLayout, QLabel, QPushButton, QScrollArea,
    QVBoxLayout, QWidget,
)

from gui_qt.icons import icon
from gui_qt.nexus_mod_card import _TwoLineLabel, wrap_tooltip
from gui_qt.safe_emit import safe_emit
from gui_qt.theme_qt import active_palette, close_button, contrast_text, _c
from Utils.wabbajack.installed import InstalledList


CARD_W, CARD_H = 300, 352
IMG_W, IMG_H = CARD_W - 2, 140


class InstalledWabbajackCard(QFrame):
    view_requested = Signal()
    remove_requested = Signal()

    def __init__(self, record: InstalledList, parent=None):
        super().__init__(parent)
        self.record = record
        self.setObjectName("InstalledWabbajackCard")
        self.setFixedSize(CARD_W, CARD_H)
        palette = active_palette()
        dim = _c(palette, "TEXT_DIM")
        self.setStyleSheet(
            f"#InstalledWabbajackCard {{ background:{_c(palette, 'BG_PANEL')};"
            f" border:1px solid {_c(palette, 'BORDER')}; border-radius:8px; }}"
            f"#InstalledWabbajackCard:hover {{ border-color:{_c(palette, 'ACCENT')}; }}"
            f"#RemoveListButton {{ background:{_c(palette, 'BTN_DANGER')};"
            f" color:{contrast_text(_c(palette, 'BTN_DANGER'))}; border:none;"
            f" border-radius:4px; padding:6px 12px; font-weight:600; }}"
            f"#RemoveListButton:hover {{ background:{_c(palette, 'BTN_DANGER_HOV')}; }}"
            f"#RemoveListButton:disabled {{ background:{_c(palette, 'BTN_GREY')};"
            f" color:{dim}; }}")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(1, 1, 1, 1)
        layout.setSpacing(0)
        self._image = QLabel(self)
        self._image.setAlignment(Qt.AlignCenter)
        self._image.setFixedSize(IMG_W, IMG_H)
        self._image.setStyleSheet(
            f"background:{_c(palette, 'BG_DEEP')}; border-radius:7px;")
        self._image.setPixmap(icon("Wabbajack.png", 72).pixmap(72, 72))
        layout.addWidget(self._image)

        body = QWidget(self)
        content = QVBoxLayout(body)
        content.setContentsMargins(10, 8, 10, 9)
        content.setSpacing(5)
        title = _TwoLineLabel(record.title, body)
        title.setFixedHeight(36)
        title.setStyleSheet(
            f"color:{_c(palette, 'TEXT_MAIN')}; font-size:13px; font-weight:600;")
        content.addWidget(title)
        metadata = record.info.get("gallery_metadata", {})
        byline = str(metadata.get("author") or self.tr("Local installation"))
        if record.info.get("version"):
            byline += " · " + str(record.info["version"])
        content.addWidget(self._line(byline, dim))
        content.addWidget(self._line(record.game_name, _c(palette, "TEXT_MAIN")))
        content.addWidget(self._line(self._status_text(record.status),
                                     _c(palette, "ACCENT")))
        profiles = (self.tr("No profiles created yet") if not record.profiles
                    else self.tr("Profiles: {0}").format(", ".join(record.profiles)))
        content.addWidget(self._line(profiles, dim))
        content.addStretch(1)

        actions = QHBoxLayout()
        view = QPushButton(self.tr("View"), body)
        view.setObjectName("GameAddBtn")
        view.setCursor(Qt.PointingHandCursor)
        view.clicked.connect(self.view_requested)
        actions.addWidget(view, 1)
        self.remove_button = QPushButton(self.tr("Remove"), body)
        self.remove_button.setObjectName("RemoveListButton")
        self.remove_button.clicked.connect(self.remove_requested)
        if record.locked_profiles:
            self.remove_button.setEnabled(False)
            self.remove_button.setToolTip(self.tr(
                "Unlock these profiles first: {0}").format(
                    ", ".join(record.locked_profiles)))
        else:
            self.remove_button.setCursor(Qt.PointingHandCursor)
        actions.addWidget(self.remove_button, 1)
        content.addLayout(actions)
        layout.addWidget(body, 1)
        self.setToolTip(wrap_tooltip(self.tr("Installation: {0}").format(
            record.directory)))

    def _line(self, text, color):
        label = QLabel(str(text), self)
        label.setTextFormat(Qt.PlainText)
        label.setFixedHeight(17)
        label.setMaximumWidth(CARD_W - 22)
        label.setToolTip(wrap_tooltip(str(text)))
        label.setStyleSheet(f"color:{color}; font-size:11px;")
        label.ensurePolished()
        label.setText(label.fontMetrics().elidedText(
            str(text), Qt.ElideRight, CARD_W - 22))
        return label

    def set_thumbnail(self, pixmap: QPixmap):
        if pixmap is not None and not pixmap.isNull():
            self._image.setPixmap(pixmap)

    def _status_text(self, status):
        labels = {
            "complete": self.tr("Installed"),
            "paused": self.tr("Paused · Resume available"),
            "cancelled": self.tr("Cancelled · Resume available"),
            "interrupted": self.tr("Interrupted · Resume available"),
            "installing": self.tr("Incomplete · Resume available"),
            "committing": self.tr("Interrupted · Resume available"),
            "published": self.tr("Incomplete · Resume available"),
        }
        return labels.get(status, self.tr("Incomplete · Resume available"))


class InstalledWabbajackView(QWidget):
    _loaded = Signal(int, object, str)
    _remove_done = Signal(object, object, str)
    _progress_update = Signal(str, int, int, str)
    _log_line = Signal(str)
    view_requested = Signal(object)
    removed = Signal(object, object)
    running_changed = Signal(bool)
    operation_progress = Signal(int, int, object)

    def __init__(self, window, *, can_remove=None, log_fn=None, parent=None):
        super().__init__(parent)
        self._window = window
        self._can_remove = can_remove or (lambda: True)
        self._log = log_fn or (lambda _message: None)
        self._records = []
        self._cards = []
        self._token = 0
        self._busy = False
        self._thumb_sequence = 0
        self._loaded.connect(self._on_loaded)
        self._remove_done.connect(self._on_remove_done)
        self._progress_update.connect(self._on_progress)
        self._log_line.connect(self._log)
        self._build()
        from gui_qt.nexus_mod_card import ThumbnailLoader
        self._thumbs = ThumbnailLoader(self, crop_w=IMG_W, crop_h=IMG_H)
        self._thumbs.loaded.connect(self._thumbnail)
        self._thumb_records = {}
        self.refresh()

    def _build(self):
        palette = active_palette()
        self.setObjectName("InstalledWabbajackView")
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        bar = QWidget(self)
        bar.setObjectName("HeaderBar")
        header = QHBoxLayout(bar)
        header.setContentsMargins(12, 8, 12, 8)
        title = QLabel(self.tr("Installed Wabbajack Lists"), bar)
        title.setStyleSheet(
            f"color:{_c(palette, 'TEXT_MAIN')}; font-weight:600; font-size:15px;")
        header.addWidget(title)
        header.addStretch(1)
        self._count = QLabel(bar)
        self._count.setStyleSheet(f"color:{_c(palette, 'TEXT_DIM')};")
        header.addWidget(self._count)
        self._refresh = QPushButton(self.tr("Refresh"), bar)
        self._refresh.setObjectName("FormButton")
        self._refresh.setCursor(Qt.PointingHandCursor)
        self._refresh.clicked.connect(self.refresh)
        header.addWidget(self._refresh)
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
        self._empty = QLabel(self.tr("Loading installed lists…"), host)
        self._empty.setAlignment(Qt.AlignCenter)
        self._empty.setWordWrap(True)
        self._empty.setMinimumHeight(220)
        self._empty.setStyleSheet(
            f"color:{_c(palette, 'TEXT_DIM')}; font-size:14px;")
        self._grid.addWidget(self._empty, 0, 0)
        self._scroll.setWidget(host)
        self._scroll.viewport().installEventFilter(self)
        root.addWidget(self._scroll, 1)
        self._status = QLabel(self)
        self._status.setWordWrap(True)
        self._status.setMargin(6)
        self._status.setTextFormat(Qt.PlainText)
        self._status.setStyleSheet(f"color:{_c(palette, 'TEXT_DIM')};")
        root.addWidget(self._status)

    def refresh(self):
        if self._busy:
            return
        self._token += 1
        token = self._token
        self._refresh.setEnabled(False)
        self._status.setText(self.tr("Scanning configured games…"))

        def worker():
            result, error = None, ""
            try:
                from Utils.wabbajack.installed import installed_lists
                result = installed_lists(
                    log=lambda message: safe_emit(self._log_line, str(message)))
            except Exception as exc:
                error = str(exc)
            safe_emit(self._loaded, token, result, error)

        threading.Thread(
            target=worker, daemon=True, name="wabbajack-installed-scan").start()

    def _on_loaded(self, token, records, error):
        if token != self._token:
            return
        self._refresh.setEnabled(not self._busy)
        if error:
            self._status.setText(self.tr(
                "Could not scan installed lists: {0}").format(error))
            return
        self._records = list(records or [])
        self._status.setText(self.tr(
            "Shared download archives and partial downloads are retained when a list is removed."))
        self._populate()

    def _populate(self):
        while self._grid.count():
            item = self._grid.takeAt(0)
            widget = item.widget()
            if widget is not None and widget is not self._empty:
                widget.hide()
                widget.deleteLater()
        self._cards = []
        self._thumb_records.clear()
        for record in self._records:
            card = InstalledWabbajackCard(record, self._scroll.widget())
            card.view_requested.connect(
                lambda record=record: self.view_requested.emit(record))
            card.remove_requested.connect(
                lambda record=record: self._confirm_remove(record))
            card.setEnabled(not self._busy)
            self._cards.append(card)
            image = str(record.info.get("gallery_metadata", {}).get("image") or "")
            if image:
                self._thumb_sequence += 1
                request_id = self._thumb_sequence
                self._thumb_records[request_id] = image
                self._thumbs.request(request_id, image)
        self._empty.setText(self.tr(
            "No managed Wabbajack installations were found across your configured games."))
        self._empty.setVisible(not self._cards)
        self._relayout()
        self._count.setText(self.tr("{0} lists").format(len(self._records)))

    def _relayout(self):
        while self._grid.count():
            self._grid.takeAt(0)
        if not self._cards:
            self._grid.addWidget(self._empty, 0, 0)
            return
        width = self._scroll.viewport().width() - 32
        columns = max(1, (width + 12) // (CARD_W + 12))
        for index, card in enumerate(self._cards):
            self._grid.addWidget(card, index // columns, index % columns)

    def _thumbnail(self, request_id, pixmap):
        wanted = self._thumb_records.get(request_id)
        if wanted is None:
            return
        for card in self._cards:
            image = str(card.record.info.get(
                "gallery_metadata", {}).get("image") or "")
            if image == wanted:
                card.set_thumbnail(pixmap)

    def _confirm_remove(self, record):
        if self._busy:
            return
        if not self._can_remove():
            self._notify(self.tr(
                "Wait for the current installation or deployment operation to finish."),
                "warning")
            return
        from gui_qt.confirm_overlay import ConfirmOverlay
        profiles = list(record.profiles) or [self.tr("No profiles created yet")]
        body = self.tr(
            "Remove '{0}' for {1}?\n\nThis permanently deletes its managed "
            "installation, installed mods, Stock Game files, saved package, "
            "work data, backups, local changes, and every linked profile.\n\n"
            "Shared download archives and partial downloads are kept.\n\n"
            "Managed directory: {2}").format(
                record.title, record.game_name, record.directory)
        ConfirmOverlay.show_over(
            self, self.tr("Remove Wabbajack List"), body,
            lambda accepted: self._start_remove(record) if accepted else None,
            confirm_label=self.tr("Remove"), list_items=profiles)

    def _start_remove(self, record):
        if self._busy or not self._can_remove():
            return
        self._busy = True
        self._refresh.setEnabled(False)
        for card in self._cards:
            card.setEnabled(False)
        self._status.setText(self.tr("Preparing to remove {0}…").format(
            record.title))
        self.running_changed.emit(True)

        def worker():
            result, error = None, ""
            try:
                from Utils.wabbajack.games import configured_games
                game = configured_games().get(record.game_name)
                if game is None:
                    raise ValueError("The configured game is no longer available")
                from Utils.wabbajack.installed import remove_installed_list
                result = remove_installed_list(
                    game, record.directory,
                    log=lambda message: safe_emit(self._log_line, str(message)),
                    progress=lambda phase, current, total, detail="":
                    safe_emit(self._progress_update, phase, current, total,
                              str(detail or "")),
                )
            except Exception as exc:
                error = str(exc)
            safe_emit(self._remove_done, record, result, error)

        threading.Thread(
            target=worker, daemon=True, name="wabbajack-installed-remove").start()

    def _on_progress(self, phase, current, total, detail):
        text = phase + (f": {detail}" if detail else "")
        self._status.setText(text)
        self.operation_progress.emit(current, total, text)

    def _on_remove_done(self, record, result, error):
        self._busy = False
        self.running_changed.emit(False)
        self._refresh.setEnabled(True)
        if error:
            self._status.setText(self.tr("Could not remove {0}: {1}").format(
                record.title, error))
            self._notify(self._status.text(), "error")
            self.refresh()
            return
        self._status.setText(self.tr("Removed {0}.").format(record.title))
        self.removed.emit(record, result)
        self.refresh()

    def _notify(self, message, state="info"):
        callback = getattr(self._window, "_notify", None)
        if callable(callback):
            callback(message, state)
        else:
            self._log(message)

    def eventFilter(self, watched, event):
        if watched is self._scroll.viewport() and event.type() == QEvent.Resize:
            self._relayout()
        return super().eventFilter(watched, event)

    def tab_close_blocked(self):
        if self._busy:
            self._status.setText(self.tr(
                "Wait for list removal to finish before closing this tab."))
        return self._busy

    def _close(self):
        tabs = getattr(self._window, "_tabs", None)
        if tabs is not None:
            tabs.close_tab("wabbajack_installed")
        else:
            self.hide()
