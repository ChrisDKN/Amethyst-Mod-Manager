"""Reusable configurable back/forward navigation for browser views."""

from __future__ import annotations

from collections.abc import Callable
from weakref import WeakSet

from PySide6.QtCore import QEvent, QObject, Qt
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import QApplication, QWidget

from gui_qt.shortcuts import (
    binding_matches_mouse, current_shortcuts, shortcut_parts,
)


_navigation_filters: WeakSet = WeakSet()


def refresh_navigation_shortcuts() -> None:
    for event_filter in tuple(_navigation_filters):
        event_filter.sync_shortcuts()


class MouseNavigationFilter(QObject):
    """Route configured back/forward bindings below *owner*."""

    def __init__(self, owner: QWidget, on_back: Callable[[], None],
                 on_forward: Callable[[], None]):
        super().__init__(owner)
        self._owner = owner
        self._callbacks = {
            "browser_back": on_back,
            "browser_forward": on_forward,
        }
        self._shortcuts = {}
        for action_id, callback in self._callbacks.items():
            shortcut = QShortcut(QKeySequence(), owner)
            shortcut.setContext(Qt.WidgetWithChildrenShortcut)
            shortcut.setAutoRepeat(False)
            shortcut.activated.connect(
                lambda cb=callback: self._activate(cb))
            self._shortcuts[action_id] = shortcut

        _navigation_filters.add(self)
        self.sync_shortcuts()
        app = QApplication.instance()
        if app is not None:
            app.installEventFilter(self)

    def sync_shortcuts(self) -> None:
        values = current_shortcuts()
        for action_id, shortcut in self._shortcuts.items():
            sequence = values.get(action_id, "")
            shortcut.setKey(QKeySequence(
                sequence if shortcut_parts(sequence) is not None else ""))

    def _activate(self, callback: Callable[[], None]) -> None:
        from gui_qt.shortcuts import _focus_is_text_input, _overlay_open

        if (_focus_is_text_input(self._owner)
                or _overlay_open(self._owner.window())):
            return
        callback()

    def eventFilter(self, watched, event):
        if event.type() != QEvent.MouseButtonPress:
            return False
        if not isinstance(watched, QWidget):
            return False
        owner = self._owner
        if watched is not owner and not owner.isAncestorOf(watched):
            return False

        for action_id, callback in self._callbacks.items():
            if binding_matches_mouse(action_id, event):
                callback()
                event.accept()
                return True
        return False
