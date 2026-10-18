"""Prevent scroll-wheel from accidentally changing widget values.

Sliders, combo boxes and spin boxes grab the wheel by default, so scrolling a
menu that happens to pass the cursor over one silently changes its value. This
installs an event filter that swallows wheel events on such widgets unless they
currently hold keyboard focus (i.e. the user deliberately clicked into them).
"""

from PySide6.QtCore import QObject, QEvent
from PySide6.QtWidgets import QWidget


class _WheelGuard(QObject):
    _instance: "_WheelGuard | None" = None

    def eventFilter(self, obj: QObject, event: QEvent) -> bool:
        if event.type() == QEvent.Wheel and isinstance(obj, QWidget):
            if not obj.hasFocus():
                event.ignore()
                return True  # swallow: don't change value, let scroll bubble up
        return super().eventFilter(obj, event)


def _guard() -> _WheelGuard:
    if _WheelGuard._instance is None:
        _WheelGuard._instance = _WheelGuard()
    return _WheelGuard._instance


def no_wheel(*widgets: QWidget) -> None:
    """Stop the given widgets from reacting to the scroll wheel unless focused.

    Also switches focus policy to click/tab-only so hovering can never grab
    wheel focus. Returns nothing; safe to pass any number of widgets.
    """
    g = _guard()
    from PySide6.QtCore import Qt
    for w in widgets:
        if w is None:
            continue
        w.setFocusPolicy(Qt.StrongFocus)
        w.installEventFilter(g)
