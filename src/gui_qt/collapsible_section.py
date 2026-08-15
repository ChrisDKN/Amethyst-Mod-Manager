"""CollapsibleSection - a titled panel whose body folds behind a ▸/▾ header.

Looks like the QGroupBox sections used by the Settings / Define-Custom-Game
views (1px border, rounded, panel background) but the whole body hides until
the header is clicked. Built as a plain QFrame + checkable QToolButton rather
than a checkable QGroupBox: a group box neither hides its children on uncheck
nor leaves room for an arrow in the title-in-margin styling.

The host view's stylesheet must style ``#CollapsibleSection`` (the frame) and
``QToolButton#SectionToggle`` (the header). The header arrow uses the app's
right.png/arrow.png icons tinted DROPDOWN_ARROW - the same pair the modlist
separators and tree delegates use.

No animation: sections live inside widgetResizable scroll areas, where a plain
show/hide relayouts correctly.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QFrame, QHBoxLayout, QToolButton, QVBoxLayout, QWidget

from gui_qt.help_marker import make_help_marker
from gui_qt.icons import icon
from gui_qt.theme_qt import bind_theme, _c

_ARROW_SZ = 12


class CollapsibleSection(QFrame):
    """Collapsed-by-default section. Fill ``self.body`` with a layout:

        sec = CollapsibleSection(title, tip)
        grid = QGridLayout(sec.body)
        ...
    """

    def __init__(self, title: str, tip: str = "", parent=None):
        super().__init__(parent)
        self.setObjectName("CollapsibleSection")
        self._title = title
        # Explicit state - body.isVisible() is False whenever an ancestor is
        # hidden (e.g. before the view is shown), so it can't back is_expanded.
        self._expanded = False

        v = QVBoxLayout(self)
        v.setContentsMargins(8, 4, 8, 6)
        v.setSpacing(6)

        head = QHBoxLayout()
        head.setContentsMargins(0, 0, 0, 0)
        head.setSpacing(6)
        self._toggle = QToolButton()
        self._toggle.setObjectName("SectionToggle")
        self._toggle.setCheckable(True)
        self._toggle.setCursor(Qt.PointingHandCursor)
        self._toggle.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self._toggle.setText(title.replace("&", "&&"))  # bare & = mnemonic
        self._toggle.toggled.connect(self.set_expanded)
        head.addWidget(self._toggle)
        if tip:
            head.addWidget(make_help_marker(tip))
        head.addStretch(1)
        v.addLayout(head)

        self.body = QWidget()
        self.body.setVisible(False)
        v.addWidget(self.body)

        bind_theme(self, roles={"DROPDOWN_ARROW"})

    def refresh_theme(self, pal: dict) -> None:
        self._arrow_color = _c(pal, "DROPDOWN_ARROW")
        self._sync_arrow()

    # ---- state -------------------------------------------------------------
    def is_expanded(self) -> bool:
        return self._expanded

    def set_expanded(self, on: bool):
        self._expanded = bool(on)
        self.body.setVisible(self._expanded)
        # Keep the button in sync when called programmatically (blockSignals
        # so a programmatic expand doesn't re-enter through toggled).
        self._toggle.blockSignals(True)
        self._toggle.setChecked(bool(on))
        self._toggle.blockSignals(False)
        self._sync_arrow()

    def expand(self):
        self.set_expanded(True)

    def _sync_arrow(self):
        # Same icon pair + tint as the modlist separators / tree delegates.
        self._toggle.setIcon(icon(
            "arrow.png" if self.is_expanded() else "right.png",
            _ARROW_SZ, color=self._arrow_color))
