"""Post-deploy launcher-wrapper instructions.

The launch handoff is produced by ``Utils.launchers.handoff`` and may contain one
field (Steam/Lutris/Faugus) or Heroic's separate executable and arguments.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QWidget, QHBoxLayout, QLabel, QPushButton, QPlainTextEdit, QCheckBox,
)

from gui_qt.overlay_base import OverlayBase
from gui_qt.theme_qt import active_palette, close_button, _c


class LaunchHandoffOverlay(OverlayBase):
    """Show launcher-specific wrapper fields after a successful deploy."""

    CARD_W = 680
    CARD_H = 470
    MIN_W = 440
    MIN_H = 340
    CLICK_OUTSIDE_CANCELS = True

    def __init__(self, host: QWidget, game_name: str, handoff, on_done=None):
        super().__init__(host, on_done=on_done)
        self._handoff = handoff
        self._areas: list[tuple[object, QPlainTextEdit, QPushButton]] = []
        p = active_palette()
        _card, v = self._make_card("LaunchHandoffCard")

        title = QLabel(self.tr("{0} - launching from {1}").format(
            game_name, handoff.launcher_name))
        title.setStyleSheet(
            f"color:{_c(p,'TEXT_MAIN')}; font-weight:600; font-size:16px;")
        v.addWidget(title)

        sub = QLabel(self.tr(
            "This deployment uses an external loader or virtual filesystem, "
            "so the launcher must start the game through Amethyst. Press Play "
            "in Amethyst, or configure {0} as follows:\n\n{1}"
        ).format(handoff.launcher_name, handoff.instructions))
        sub.setStyleSheet(f"color:{_c(p,'TEXT_DIM')}; font-size:13px;")
        sub.setWordWrap(True)
        v.addWidget(sub)

        for field in handoff.fields:
            label = QLabel(field.label)
            label.setStyleSheet(
                f"color:{_c(p,'TEXT_MAIN')}; font-weight:600; font-size:12px;")
            v.addWidget(label)

            row = QHBoxLayout()
            area = QPlainTextEdit()
            area.setReadOnly(True)
            area.setMaximumHeight(72)
            area.setLineWrapMode(QPlainTextEdit.WidgetWidth)
            area.setStyleSheet(
                f"QPlainTextEdit {{ background:{_c(p,'BG_DEEP')};"
                f" color:{_c(p,'TEXT_MAIN')}; border:1px solid {_c(p,'BORDER')};"
                f" border-radius:5px; padding:6px; font-family:monospace; }}")
            area.setPlainText(field.value)
            row.addWidget(area, 1)

            copy = QPushButton(self.tr("Copy"))
            copy.setObjectName("FormButton")
            copy.setCursor(Qt.PointingHandCursor)
            copy.clicked.connect(
                lambda _checked=False, f=field, b=copy: self._copy(f.value, b))
            row.addWidget(copy)
            v.addLayout(row)
            self._areas.append((field, area, copy))

        note = QLabel(handoff.note)
        note.setStyleSheet(f"color:{_c(p,'TEXT_DIM')}; font-size:11px;")
        note.setWordWrap(True)
        v.addWidget(note)

        self._hide_chk = QCheckBox(self.tr("Don't show this again for {0}").format(
            handoff.launcher_name))
        self._hide_chk.setStyleSheet(f"color:{_c(p,'TEXT_DIM')}; font-size:11px;")
        v.addWidget(self._hide_chk)

        bar = QHBoxLayout()
        bar.addStretch(1)
        close = close_button(self.tr("Close"), pal=p)
        close.clicked.connect(self._close)
        bar.addWidget(close)
        v.addLayout(bar)

        self._present()
        if len(self._areas) == 1:
            field, _area, button = self._areas[0]
            self._copy(field.value, button)

    def _copy(self, value: str, button: QPushButton) -> None:
        cb = QGuiApplication.clipboard()
        if cb is not None:
            cb.setText(value)
            for _field, _area, other in self._areas:
                other.setText(self.tr("Copy"))
            button.setText(self.tr("Copied ✓"))
        else:
            button.setText(self.tr("Copy failed"))

    def _close(self) -> None:
        self._finish(bool(self._hide_chk.isChecked()))
