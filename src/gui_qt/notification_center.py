"""Header notifications button: unread dot + dropdown history of recent toasts."""

from __future__ import annotations

from collections import deque
from datetime import datetime

from PySide6.QtCore import Qt, QObject, QPoint, QSize, Signal
from PySide6.QtGui import QAction, QColor, QPainter
from PySide6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QMenu, QScrollArea, QToolButton, QVBoxLayout,
    QWidget, QWidgetAction,
)

from gui_qt.icons import icon
from gui_qt.theme_qt import active_palette, _c

MAX_ENTRIES = 100

# Per-state severity colours - same palette keys as the toast dot QSS.
_STATE_COLOR_KEYS = {
    "info": "ACCENT",
    "success": "TEXT_OK_BRIGHT",
    "warning": "TEXT_WARN_BRIGHT",
    "error": "STATUS_ERR_BRIGHT",
}

_MENU_W = 400          # dropdown content width
_MENU_MAX_H = 380      # cap before the list scrolls (~10 rows)


class NotificationHistory(QObject):
    """Session-only ring buffer of toast texts. UI-thread only, like the
    NotificationManager that feeds it."""

    entry_added = Signal()
    changed = Signal()

    def __init__(self, parent: QObject | None = None):
        super().__init__(parent)
        self._entries: deque = deque(maxlen=MAX_ENTRIES)

    def add(self, text: str, state: str = "info") -> None:
        if state not in _STATE_COLOR_KEYS:
            state = "info"
        self._entries.append((text, state, datetime.now()))
        self.entry_added.emit()
        self.changed.emit()

    def entries(self) -> list:
        """Newest-first (text, state, when) tuples."""
        return list(reversed(self._entries))

    def clear(self) -> None:
        self._entries.clear()
        self.changed.emit()


class NotificationButton(QToolButton):
    """Square header button (matches the Settings button) popping the
    notification history as a menu; shows an accent dot while unread."""

    def __init__(self, history: NotificationHistory, btn_h: int = 42,
                 icon_px: int = 24, parent: QWidget | None = None):
        super().__init__(parent)
        self._history = history
        self._unread = False
        pal = active_palette()
        self.setIcon(icon("notification.png", icon_px,
                          color=_c(pal, "TEXT_MAIN")))
        self.setIconSize(QSize(icon_px, icon_px))
        self.setToolButtonStyle(Qt.ToolButtonIconOnly)
        self.setObjectName("IconButton")
        self.setCursor(Qt.PointingHandCursor)
        self.setFixedSize(btn_h, btn_h)
        self.setToolTip(self.tr("Notifications"))
        history.entry_added.connect(self._on_entry_added)
        self.clicked.connect(self._show_menu)

    # -- unread dot ----------------------------------------------------------
    def _on_entry_added(self):
        self._unread = True
        self.update()

    def _mark_read(self):
        self._unread = False
        self.update()

    def paintEvent(self, event):
        super().paintEvent(event)
        if not self._unread:
            return
        pal = active_palette()
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(Qt.NoPen)
        center = QPoint(self.width() - 12, 12)
        # Background-coloured halo so the dot separates from the glyph.
        p.setBrush(QColor(_c(pal, "BG_ROW")))
        p.drawEllipse(center, 6, 6)
        p.setBrush(QColor(_c(pal, "ACCENT")))
        p.drawEllipse(center, 4, 4)

    # -- menu ----------------------------------------------------------------
    def _show_menu(self):
        menu = self._build_menu()
        menu.adjustSize()
        anchor = self.mapToGlobal(self.rect().bottomRight())
        x = max(anchor.x() - menu.sizeHint().width(), 0)
        menu.exec(QPoint(x, anchor.y()))

    def _build_menu(self) -> QMenu:
        """Fresh right-aligned menu: newest-first rows + a Clear-all action.
        Also clears the unread dot (built = seen)."""
        self._mark_read()
        menu = QMenu(self)
        entries = self._history.entries()
        if not entries:
            empty = menu.addAction(self.tr("No notifications"))
            empty.setEnabled(False)
        else:
            wa = QWidgetAction(menu)
            wa.setDefaultWidget(self._entries_widget(entries))
            menu.addAction(wa)
        menu.addSeparator()
        clear = QAction(self.tr("Clear all"), menu)
        clear.setEnabled(bool(entries))
        clear.triggered.connect(self._history.clear)
        menu.addAction(clear)
        return menu

    def _entries_widget(self, entries) -> QScrollArea:
        """Scrollable column of notification rows, capped at ~10 visible."""
        pal = active_palette()
        col = QWidget()
        col.setFixedWidth(_MENU_W)
        v = QVBoxLayout(col)
        v.setContentsMargins(6, 4, 6, 4)
        v.setSpacing(6)
        for text, state, when in entries:
            v.addWidget(self._entry_row(pal, text, state, when))
        scroll = QScrollArea()
        scroll.setWidget(col)
        scroll.setWidgetResizable(False)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        scroll.setStyleSheet(
            "QScrollArea, QScrollArea > QWidget > QWidget"
            " { background: transparent; }")
        col.adjustSize()
        sbar_w = scroll.verticalScrollBar().sizeHint().width()
        scroll.setFixedWidth(_MENU_W + sbar_w + 2)
        scroll.setFixedHeight(min(col.sizeHint().height(), _MENU_MAX_H))
        return scroll

    def _entry_row(self, pal, text: str, state: str, when) -> QFrame:
        row = QFrame()
        h = QHBoxLayout(row)
        h.setContentsMargins(6, 2, 6, 2)
        h.setSpacing(10)
        dot = QLabel("●")
        dot.setStyleSheet(
            f"font-size:14px; color: {_c(pal, _STATE_COLOR_KEYS[state])};")
        dot.setAlignment(Qt.AlignTop)
        h.addWidget(dot, 0, Qt.AlignTop)
        label = QLabel(text)
        label.setWordWrap(True)
        label.setStyleSheet("font-size:13px;")
        label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        h.addWidget(label, 1)
        ts = QLabel(when.strftime("%H:%M"))
        ts.setStyleSheet(f"font-size:12px; color: {_c(pal, 'TEXT_DIM')};")
        h.addWidget(ts, 0, Qt.AlignTop)
        return row
