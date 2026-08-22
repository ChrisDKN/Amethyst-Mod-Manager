"""Reusable Mouse 4/5 navigation for child-heavy Qt views."""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import QEvent, QObject, Qt
from PySide6.QtWidgets import QApplication, QWidget


class MouseNavigationFilter(QObject):
    """Route side-button presses anywhere below *owner* to two callbacks.

    Installing the filter on the application is intentional: cards, buttons and
    scroll-area viewports receive mouse events before their containing browser,
    and some of those widgets consume the event instead of propagating it.
    Events outside *owner* are ignored.
    """

    def __init__(self, owner: QWidget, on_back: Callable[[], None],
                 on_forward: Callable[[], None]):
        super().__init__(owner)
        self._owner = owner
        self._on_back = on_back
        self._on_forward = on_forward
        app = QApplication.instance()
        if app is not None:
            app.installEventFilter(self)

    def eventFilter(self, watched, event):
        if event.type() != QEvent.MouseButtonPress:
            return False
        if not isinstance(watched, QWidget):
            return False
        owner = self._owner
        if watched is not owner and not owner.isAncestorOf(watched):
            return False

        button = event.button()
        if button == Qt.BackButton:       # Mouse 4
            self._on_back()
        elif button == Qt.ForwardButton:  # Mouse 5
            self._on_forward()
        else:
            return False
        event.accept()
        return True
