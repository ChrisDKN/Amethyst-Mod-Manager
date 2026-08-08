"""LoadingOverlay - a translucent overlay shown over a panel while it loads.

A small reusable widget that shows the application logo centered over its parent
widget as a static "loading…" indicator. Call ``show_over(parent)`` to (re)parent
+ resize it to cover *parent* and show it; ``hide_overlay()`` hides it.

Used by the card-grid views (Add-Game, Nexus browser, Collections browser) and
the Data / Text Files tab trees to give visual feedback while their contents
fetch on a worker thread. A static
image is used (not an animated gif) because the animation can't be driven
reliably while the GUI thread is busy building the panel's contents.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import QWidget, QLabel, QVBoxLayout

_LOGO = Path(__file__).resolve().parent.parent / "icons" / "Logo.png"
_LOGO_SIZE = 160


class LoadingOverlay(QWidget):
    """A frameless, semi-transparent overlay showing the app logo + "Loading…".

    Create it as a child of the widget it should cover (typically a scroll
    area). It repositions itself to fill that widget whenever it is shown, and
    tracks the parent's resize events so it stays covering.
    """

    def __init__(self, parent: QWidget):
        super().__init__(parent)
        self.setObjectName("LoadingOverlay")
        # Dim the panel behind the logo. WA_StyledBackground + a translucent
        # stylesheet keeps whatever's underneath faintly visible.
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setStyleSheet("#LoadingOverlay { background: rgba(0, 0, 0, 90); }")

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(12)
        lay.setAlignment(Qt.AlignCenter)

        self._logo = QLabel()
        self._logo.setAlignment(Qt.AlignCenter)
        pm = QPixmap(str(_LOGO)) if _LOGO.is_file() else QPixmap()
        if not pm.isNull():
            self._logo.setPixmap(pm.scaled(
                _LOGO_SIZE, _LOGO_SIZE,
                Qt.KeepAspectRatio, Qt.SmoothTransformation))
        lay.addWidget(self._logo)

        self._text = QLabel(self.tr("Loading…"))
        self._text.setAlignment(Qt.AlignCenter)
        self._text.setStyleSheet("color: white; font-size: 15px; font-weight: 600;")
        lay.addWidget(self._text)

        parent.installEventFilter(self)
        self.hide()

    def show_over(self, parent: QWidget | None = None) -> None:
        """Reparent to *parent* (or keep the current one) and cover it."""
        if parent is not None and parent is not self.parent():
            self.setParent(parent)
            parent.installEventFilter(self)
        self._resize_to_parent()
        self.raise_()
        self.show()
        from PySide6.QtCore import QTimer
        QTimer.singleShot(0, self._resize_to_parent)

    def hide_overlay(self) -> None:
        self.hide()

    def _resize_to_parent(self) -> None:
        p = self.parentWidget()
        if p is not None:
            self.setGeometry(0, 0, p.width(), p.height())

    def eventFilter(self, obj, event):
        from PySide6.QtCore import QEvent
        if obj is self.parentWidget() \
                and event.type() in (QEvent.Resize, QEvent.Show) \
                and self.isVisible():
            self._resize_to_parent()
        return super().eventFilter(obj, event)
