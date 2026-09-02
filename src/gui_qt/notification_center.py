"""Header notifications button: active progress + recent toast history."""

from __future__ import annotations

from collections import deque
from datetime import datetime
from weakref import ref, WeakSet

from PySide6.QtCore import Qt, QObject, QPoint, QSize, Signal, QTimer
from PySide6.QtGui import QAction, QColor, QPainter
from PySide6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QMenu, QProgressBar, QPushButton, QScrollArea,
    QTabWidget, QToolButton, QVBoxLayout, QWidget, QWidgetAction,
)

from gui_qt.icons import icon
from gui_qt.theme_qt import active_palette, bind_theme, _c

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
_UNCHANGED = object()


class _ProgressRow(QFrame):
    """Live progress item pinned above the notification-history scroller."""

    def __init__(self, owner: "NotificationButton", key: str):
        super().__init__()
        self._owner = owner
        self._key = key
        self.setObjectName("NotificationProgressRow")
        pal = active_palette()
        self.setStyleSheet(
            f"#NotificationProgressRow {{ background:{_c(pal, 'BG_PANEL')};"
            f" border:1px solid {_c(pal, 'BORDER')}; border-radius:6px; }}")

        v = QVBoxLayout(self)
        v.setContentsMargins(10, 8, 10, 8)
        v.setSpacing(6)
        title_row = QHBoxLayout()
        title_row.setContentsMargins(0, 0, 0, 0)
        title_row.setSpacing(8)
        self._title = QLabel()
        self._title.setStyleSheet("font-size:14px; font-weight:600;")
        title_row.addWidget(self._title, 1)
        self._cancel = QPushButton(self.tr("Cancel"))
        self._cancel.setObjectName("DangerButton")
        self._cancel.setCursor(Qt.PointingHandCursor)
        self._cancel.clicked.connect(
            lambda: self._owner._cancel_progress(self._key))
        title_row.addWidget(self._cancel)
        v.addLayout(title_row)

        self._phase = QLabel()
        self._phase.setWordWrap(True)
        self._phase.setStyleSheet(
            f"font-size:12px; color:{_c(pal, 'TEXT_DIM')};")
        v.addWidget(self._phase)
        self._bar = QProgressBar()
        self._bar.setTextVisible(False)
        self._bar.setFixedHeight(10)
        v.addWidget(self._bar)
        self._count = QLabel()
        self._count.setAlignment(Qt.AlignRight)
        self._count.setStyleSheet(
            f"font-size:12px; color:{_c(pal, 'TEXT_DIM')};")
        v.addWidget(self._count)

    def update_progress(self, entry: dict) -> None:
        self._title.setText(entry.get("title") or self.tr("Working"))
        self._phase.setText(entry.get("phase") or self.tr("Working…"))
        done = max(0, int(entry.get("done") or 0))
        total = max(0, int(entry.get("total") or 0))
        if total > 0:
            bar_done, bar_total = min(done, total), total
            while bar_total > 0x7FFFFFFF:
                bar_done >>= 10
                bar_total >>= 10
            self._bar.setRange(0, bar_total)
            self._bar.setValue(bar_done)
            if entry.get("bytes_mode"):
                from Utils.downloads.cache import format_size
                self._count.setText(self.tr("{0} / {1}").format(
                    format_size(min(done, total)), format_size(total)))
            else:
                self._count.setText(self.tr("{0} / {1}").format(
                    min(done, total), total))
        else:
            self._bar.setRange(0, 0)
            self._count.clear()

        cancelling = bool(entry.get("cancelling"))
        callback = entry.get("cancel")
        self._cancel.setVisible(callable(callback) or cancelling)
        self._cancel.setEnabled(callable(callback) and not cancelling)
        self._cancel.setText(
            self.tr("Cancelling…") if cancelling
            else entry.get("cancel_label") or self.tr("Cancel"))


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


class _NotificationMirrorButton(QToolButton):
    """Secondary entry point sharing a NotificationButton's state and menu."""

    def __init__(self, source: "NotificationButton", btn_h: int, icon_px: int,
                 parent: QWidget | None = None):
        super().__init__(parent)
        self._source = source
        self._icon_px = icon_px
        self._icon_ratio = icon_px / btn_h
        self.setIconSize(QSize(icon_px, icon_px))
        self.setToolButtonStyle(Qt.ToolButtonIconOnly)
        # Same tile as the header button (see the global #IconButton QSS) - the
        # strip behind it supplies the tab-bar background the header has.
        self.setObjectName("IconButton")
        self.setCursor(Qt.PointingHandCursor)
        self.setFixedSize(btn_h, btn_h)
        self.setToolTip(self.tr("Notifications"))
        # Routed through self._source (not the ctor arg) so rebind() re-aims
        # the click without having to reconnect the signal.
        self.clicked.connect(lambda: self._source._toggle_menu(self))
        self.refresh_theme(active_palette())

    def rebind(self, source: "NotificationButton") -> None:
        """Back this mirror with a different button (the old one was replaced)."""
        self._source = source
        self.update()

    def sizeHint(self) -> QSize:
        # setFixedSize() clamps resizes but leaves sizeHint() at the style's
        # value.  A QTabWidget corner is laid out from the hint ALONE, so a
        # smaller hint places the widget as if it were small and the fixed size
        # then overflows the corner rect - the button hangs off the window edge
        # and gets clipped.  Report what we are actually going to be.
        return self.minimumSize()

    def set_tile_size(self, px: int) -> None:
        """Resize the tile, keeping the glyph's share of it."""
        if px == self.height():
            return
        self._icon_px = max(12, round(px * self._icon_ratio))
        self.setFixedSize(px, px)
        self.setIconSize(QSize(self._icon_px, self._icon_px))
        self.refresh_theme(active_palette())

    def refresh_theme(self, pal: dict) -> None:
        self.setIcon(icon("notification.png", self._icon_px,
                          color=_c(pal, "TEXT_MAIN")))
        self.update()

    def paintEvent(self, event):
        super().paintEvent(event)
        self._source._paint_badge(self)


class _TabNotificationStrip(QWidget):
    """Tab-bar-coloured host for a mirror button used as a QTabWidget corner."""

    # QTabWidget paints nothing behind a corner widget, so a bare button there
    # hangs over the window background - a dark hole beside the tab strip. This
    # fills the corner with the tab bar's own colour and insets the button from
    # the window edge.
    _PAD_L, _PAD_R = 8, 10
    _PAD_V = 3       # matches the tab tiles' top margin, so the two line up
    _MAX_TILE = 42   # never outgrow the header button

    def __init__(self, button: _NotificationMirrorButton,
                 parent: QWidget | None = None):
        super().__init__(parent)
        self._button = button
        self.setObjectName("TabCornerStrip")
        # A plain QWidget ignores a stylesheet background without this.
        self.setAttribute(Qt.WA_StyledBackground, True)
        h = QHBoxLayout(self)
        h.setContentsMargins(self._PAD_L, 0, self._PAD_R, 0)
        h.setSpacing(0)
        h.addWidget(button, 0, Qt.AlignVCenter)
        bind_theme(self, roles={"BG_HEADER"})

    def refresh_theme(self, pal: dict) -> None:
        self.setStyleSheet(
            f"#TabCornerStrip {{ background: {_c(pal, 'BG_HEADER')}; }}")

    def sizeHint(self) -> QSize:
        # The corner is laid out at exactly this size, so it must cover the
        # button: a smaller hint would leave the (fixed-size) button overflowing
        # the corner rect and clipped against the window edge.
        return QSize(self._button.width() + self._PAD_L + self._PAD_R,
                     max(self._button.height(), self._row_height()))

    def _row_height(self) -> int:
        tabs = self.parent()
        if isinstance(tabs, QTabWidget) and tabs.tabBar() is not None:
            return max(tabs.tabBar().height(), tabs.tabBar().sizeHint().height())
        return 0

    def moveEvent(self, event):
        super().moveEvent(event)
        self._fill_row()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._fill_row()

    def _fill_row(self) -> None:
        """Snap to the tab-bar row and grow the button to fill it."""
        row_h = self._row_height()
        if not row_h:
            return
        # The row's real height is only known once the bar is laid out (it grows
        # with the theme's font/padding), so the tile is fitted here rather than
        # hardcoded. updateGeometry() re-runs the corner layout for the new width.
        tile = max(24, min(self._MAX_TILE, row_h - 2 * self._PAD_V))
        if tile != self._button.height():
            self._button.set_tile_size(tile)
            self.updateGeometry()
        # Qt bottom-aligns a corner widget in the row and shaves the style's tab
        # base overlap off its top, leaving an unpainted sliver of window
        # background above the strip.
        if self.y() != 0 or self.height() != row_h:
            self.setGeometry(self.x(), 0, self.width(), row_h)


class NotificationButton(QToolButton):
    """Square header button (matches the Settings button) popping the
    notification history as a menu; shows an accent dot while unread."""

    def __init__(self, history: NotificationHistory, btn_h: int = 42,
                 icon_px: int = 24, parent: QWidget | None = None):
        super().__init__(parent)
        self._history = history
        self._unread = False
        self._busy = False          # tracks _progress for badge repaints
        self._progress: dict[str, dict] = {}
        self._active_menu: QMenu | None = None
        self._menu_anchor = None
        self._progress_action = None
        self._progress_separator = None
        self._progress_box = None
        self._progress_layout = None
        self._progress_rows: dict[str, _ProgressRow] = {}
        self._history_scroll = None
        self._history_action = None
        self._mirrors: WeakSet = WeakSet()
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
        self._icon_px = icon_px
        bind_theme(self, roles={"TEXT_MAIN"})

    def refresh_theme(self, pal: dict) -> None:
        self.setIcon(icon("notification.png", self._icon_px,
                          color=_c(pal, "TEXT_MAIN")))
        self.update()
        for mirror in list(self._mirrors):
            mirror.refresh_theme(pal)

    def create_mirror(self, btn_h: int = 34, icon_px: int = 20,
                      parent: QWidget | None = None) -> QWidget:
        """Create another button backed by this button's live menu state.
        Returns the strip hosting it - that is the widget to place."""
        mirror = _NotificationMirrorButton(self, btn_h, icon_px)
        self._mirrors.add(mirror)
        return _TabNotificationStrip(mirror, parent)

    def adopt_mirrors(self, previous: "NotificationButton") -> None:
        """Re-point *previous*'s mirrors at this button."""
        # The toolbar can be rebuilt in place (moved to a side bar), which
        # replaces this button while the tab-row mirror outlives it. Left
        # attached to the discarded button the mirror stops getting badge
        # updates and its menu is backed by a dead widget.
        for mirror in list(previous._mirrors):
            mirror.rebind(self)
            self._mirrors.add(mirror)
        previous._mirrors.clear()

    # -- badge ---------------------------------------------------------------
    def _on_entry_added(self):
        self._unread = True
        self._refresh_badges()

    def _mark_read(self):
        self._unread = False
        self._refresh_badges()

    def _refresh_badges(self) -> None:
        self.update()
        for mirror in list(self._mirrors):
            mirror.update()

    def paintEvent(self, event):
        super().paintEvent(event)
        self._paint_badge(self)

    def _paint_badge(self, target: QWidget) -> None:
        """Dot over the bell: work in progress outranks the unread marker."""
        if self._progress:
            key = "STATUS_DL_GREEN"
        elif self._unread:
            key = "ACCENT"
        else:
            return
        pal = active_palette()
        p = QPainter(target)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(Qt.NoPen)
        edge = 12 if target.width() >= 40 else 9
        center = QPoint(target.width() - edge, edge)
        # Background-coloured halo so the dot separates from the glyph.
        p.setBrush(QColor(_c(pal, "BG_ROW")))
        p.drawEllipse(center, 6, 6)
        p.setBrush(QColor(_c(pal, key)))
        p.drawEllipse(center, 4, 4)

    # -- menu ----------------------------------------------------------------
    def _show_menu(self):
        self._toggle_menu(self)

    def _toggle_menu(self, anchor: QWidget):
        if self._active_menu is not None and self._active_menu.isVisible():
            self._active_menu.close()
            return
        self.open_menu(anchor)

    @staticmethod
    def _menu_origin(anchor: QWidget, size: QSize) -> QPoint:
        """Top-left for the menu: below-left of *anchor*, flipped to stay on
        screen (a left-hand side bar has no room to its left)."""
        rect = anchor.rect()
        below_left = anchor.mapToGlobal(rect.bottomRight())
        screen = anchor.screen()
        if screen is None:
            return QPoint(max(below_left.x() - size.width(), 0), below_left.y())
        area = screen.availableGeometry()
        x = below_left.x() - size.width()
        if x < area.left():
            # Not enough room to the left - hang it off the anchor's right edge
            # instead, and only then clamp (a menu wider than the screen).
            x = anchor.mapToGlobal(rect.topLeft()).x()
            x = min(x, area.right() - size.width())
            x = max(x, area.left())
        y = below_left.y()
        if y + size.height() > area.bottom():
            # Drop the menu above the button rather than run off the bottom.
            above = anchor.mapToGlobal(rect.topLeft()).y() - size.height()
            y = above if above >= area.top() else max(area.bottom() - size.height(),
                                                      area.top())
        return QPoint(x, y)

    def open_menu(self, anchor: QWidget | None = None):
        """Open the live notification menu without blocking the caller."""
        if self._active_menu is not None:
            return
        window = (anchor or self).window()
        if (window is None or not window.isVisible() or window.isMinimized()
                or not window.isActiveWindow()):
            return
        if anchor is None or not anchor.isVisible():
            if self.isVisible():
                anchor = self
            else:
                anchor = next(
                    (m for m in list(self._mirrors) if m.isVisible()), self)
        menu = self._build_menu()
        menu.adjustSize()
        self._active_menu = menu
        self._menu_anchor = ref(anchor)

        def _closed(m=menu):
            if self._active_menu is m:
                self._active_menu = None
                self._menu_anchor = None
                self._progress_action = None
                self._progress_separator = None
                self._progress_box = None
                self._progress_layout = None
                self._progress_rows = {}
                self._history_scroll = None
                self._history_action = None
            m.deleteLater()

        menu.aboutToHide.connect(_closed)
        menu.popup(self._menu_origin(anchor, menu.sizeHint()))
        # Re-anchor off the laid-out width now that popup() has shown it, so the
        # initial placement can't disagree with later re-anchoring.
        self._place_menu()

    def _place_menu(self) -> None:
        """Keep the open menu anchored to the button as its size changes."""
        menu = self._active_menu
        anchor = self._menu_anchor() if self._menu_anchor is not None else None
        if menu is None or not menu.isVisible() or anchor is None \
                or not anchor.isVisible():
            return
        # Same rule as the initial popup - re-anchoring must not undo the flip
        # that keeps the menu on screen beside a side bar.
        menu.move(self._menu_origin(anchor, menu.size()))

    def set_progress(self, key: str, done: int, total: int,
                     phase: str | None = None, title: str | None = None,
                     bytes_mode: bool = False, cancel_callback=_UNCHANGED,
                     cancel_label: str | None = None,
                     auto_open: bool = False) -> None:
        """Add or update a live progress item pinned above notifications.

        ``auto_open`` only opens for a newly-created item, so progress updates
        never reopen a menu that the user deliberately closed.
        """
        is_new = key not in self._progress
        entry = self._progress.setdefault(key, {
            "cancel": None, "cancelling": False,
        })
        entry.update({
            "done": int(done), "total": int(total),
            "phase": phase or "", "title": title or self.tr("Working"),
            "bytes_mode": bool(bytes_mode),
            "cancel_label": cancel_label or self.tr("Cancel"),
        })
        if cancel_callback is not _UNCHANGED:
            if callable(cancel_callback):
                entry["cancel"] = cancel_callback
                entry["cancelling"] = False
            elif not entry.get("cancelling"):
                entry["cancel"] = None
        self._sync_progress_widget()
        if auto_open and is_new:
            QTimer.singleShot(0, self.open_menu)

    def clear_progress(self, key: str) -> None:
        self._progress.pop(key, None)
        self._sync_progress_widget()

    def _cancel_progress(self, key: str) -> None:
        entry = self._progress.get(key)
        callback = entry.get("cancel") if entry is not None else None
        if not callable(callback):
            return
        entry["cancel"] = None
        entry["cancelling"] = True
        self._sync_progress_widget()
        try:
            callback()
        except Exception:
            # A cancellation callback is best-effort UI plumbing. Its worker
            # owns error reporting and completion cleanup.
            pass

    def _progress_widget(self) -> QWidget:
        box = QWidget()
        box.setFixedWidth(_MENU_W)
        layout = QVBoxLayout(box)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(6)
        self._progress_box = box
        self._progress_layout = layout
        self._progress_rows = {}
        self._sync_progress_widget()
        return box

    def _sync_progress_widget(self) -> None:
        layout = self._progress_layout
        progress_height_changed = False
        if layout is not None:
            for key in list(self._progress_rows):
                if key in self._progress:
                    continue
                row = self._progress_rows.pop(key)
                # deleteLater() leaves the widget alive until control returns to
                # Qt.  Hide it now so a download row cannot keep painting over
                # the extraction row that replaces it in the same event turn.
                row.hide()
                layout.removeWidget(row)
                row.deleteLater()
            for key, entry in self._progress.items():
                row = self._progress_rows.get(key)
                if row is None:
                    row = _ProgressRow(self, key)
                    self._progress_rows[key] = row
                    layout.addWidget(row)
                row.update_progress(entry)
                # A child added to an already-visible QWidgetAction remains
                # explicitly hidden until the next event-loop pass.  Showing it
                # here makes QLayout include it in the size calculation below.
                row.show()
            if self._progress_box is not None:
                layout.invalidate()
                width = self._progress_box.width() or _MENU_W
                height = layout.heightForWidth(width) \
                    if layout.hasHeightForWidth() else -1
                if height < 0:
                    height = layout.sizeHint().height()
                height = max(0, height)
                progress_height_changed = \
                    height != self._progress_box.height()
                self._progress_box.setFixedHeight(height)
                self._progress_box.updateGeometry()
        visible = bool(self._progress)
        if self._progress_action is not None:
            # QMenu caches QWidgetAction geometry while it is open.  Merely
            # resizing the default widget (or calling menu.adjustSize()) does
            # not invalidate that cache, so a newly concurrent download and
            # extraction get compressed into the old one-row slot.  A
            # synchronous visibility flip invalidates the action geometry
            # without a visible flash because painting happens later.
            if visible and progress_height_changed \
                    and self._progress_action.isVisible():
                self._progress_action.setVisible(False)
            self._progress_action.setVisible(visible)
        if self._progress_separator is not None:
            self._progress_separator.setVisible(visible)
        self._resize_history_scroll()
        if self._active_menu is not None and self._active_menu.isVisible():
            # Progress rows appearing/finishing change the menu's width, so it
            # has to be re-anchored or it drifts away from the button.
            self._active_menu.adjustSize()
            self._place_menu()
        # Repaint on the busy flip only - this runs on every progress tick.
        became_idle = self._busy and not visible
        if visible != self._busy:
            self._busy = visible
            self._refresh_badges()
        if became_idle and self._active_menu is not None \
                and self._active_menu.isVisible():
            # Download completion commonly hands the archive straight to the
            # installer.  Defer the close by one event turn so the extraction
            # row can replace it without a close/reopen flicker.  The callback
            # closes only this menu instance and only if no live work appeared.
            menu = self._active_menu
            QTimer.singleShot(0, lambda m=menu: self._close_menu_if_idle(m))

    def _close_menu_if_idle(self, menu: QMenu) -> None:
        if not self._progress and self._active_menu is menu \
                and menu.isVisible():
            menu.close()

    def _resize_history_scroll(self) -> None:
        scroll = self._history_scroll
        if scroll is None or scroll.widget() is None:
            return
        # Keep the complete menu close to its old maximum height when one or
        # two pinned operations are present; the history remains independently
        # scrollable beneath them.
        max_h = max(140, _MENU_MAX_H - min(len(self._progress), 2) * 100)
        height = min(scroll.widget().sizeHint().height(), max_h)
        height_changed = height != scroll.height()
        scroll.setFixedHeight(height)
        scroll.updateGeometry()
        # Like the live-progress QWidgetAction above, QMenu caches the history
        # action's height while open.  Invalidate it when the scroller grows or
        # shrinks; otherwise its viewport can paint over the Clear-all footer.
        if height_changed and self._history_action is not None \
                and self._history_action.isVisible():
            self._history_action.setVisible(False)
            self._history_action.setVisible(True)

    def _build_menu(self) -> QMenu:
        """Fresh right-aligned menu: newest-first rows + a Clear-all action.
        Also clears the unread dot (built = seen)."""
        self._mark_read()
        self._history_scroll = None
        self._history_action = None
        menu = QMenu(self)
        progress = QWidgetAction(menu)
        progress.setDefaultWidget(self._progress_widget())
        menu.addAction(progress)
        progress_sep = menu.addSeparator()
        self._progress_action = progress
        self._progress_separator = progress_sep
        progress.setVisible(bool(self._progress))
        progress_sep.setVisible(bool(self._progress))
        entries = self._history.entries()
        if not entries:
            empty = menu.addAction(self.tr("No notifications"))
            empty.setEnabled(False)
        else:
            wa = QWidgetAction(menu)
            wa.setDefaultWidget(self._entries_widget(entries))
            menu.addAction(wa)
            self._history_action = wa
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
        self._history_scroll = scroll
        self._resize_history_scroll()
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
