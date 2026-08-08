"""Inline 'new profile' bar shown above the modlist when the user picks
'Add new profile…'. Qt port of the Tk `modlist_panel._build_new_profile_bar` +
`show_new_profile_bar`. A name field, a 'Use Profile Specific Mods' checkbox, and
Create / Cancel buttons.

The bar is data-thin: it calls ``on_create(name, profile_specific_mods)`` when the
user confirms (the app validates + creates the profile) and ``on_cancel`` when
dismissed. Enter = create, Escape = cancel. Hidden by default; ``open_for`` resets
and reveals it.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QWidget, QHBoxLayout, QLabel, QLineEdit, QCheckBox, QPushButton,
)

from gui_qt.theme_qt import active_palette, _c


class NewProfileBar(QWidget):
    """Inline create-profile row. ``on_create(name, profile_specific_mods)`` and
    ``on_cancel()`` are set by the host (defaults are no-ops)."""

    def __init__(self, on_create=None, on_cancel=None, parent=None):
        super().__init__(parent)
        self.on_create = on_create or (lambda name, specific: None)
        self.on_cancel = on_cancel or (lambda: None)
        self.setObjectName("NewProfileBar")
        self._build()
        self.setVisible(False)

    def _build(self):
        p = active_palette()
        # Use the lightest neutral grey in the palette (BG_ROW_HOVER) so the bar
        # reads clearly grey against the near-black header/list rather than
        # blending into it.
        self.setStyleSheet(
            f"#NewProfileBar {{ background: {_c(p,'BG_ROW_HOVER')};"
            f" border-bottom: 1px solid {_c(p,'BORDER')}; }}")
        h = QHBoxLayout(self)
        h.setContentsMargins(8, 6, 8, 6)
        h.setSpacing(8)

        lbl = QLabel(self.tr("New profile:"))
        lbl.setStyleSheet(f"color: {_c(p,'TEXT_MAIN')};")
        h.addWidget(lbl)

        self._name = QLineEdit()
        self._name.setPlaceholderText(self.tr("Profile name"))
        self._name.setFixedWidth(200)
        self._name.returnPressed.connect(self._confirm)
        h.addWidget(self._name)

        self._specific = QCheckBox(self.tr("Use Profile Specific Mods"))
        self._specific.setToolTip(
            self.tr("Profiles with this setting use their own mods folders"))
        self._specific.setStyleSheet(f"color: {_c(p,'TEXT_MAIN')};")
        h.addWidget(self._specific)

        h.addStretch(1)

        create = QPushButton(self.tr("Create"))
        create.setObjectName("PrimaryButton")
        create.setCursor(Qt.PointingHandCursor)
        create.clicked.connect(self._confirm)
        h.addWidget(create)

        cancel = QPushButton(self.tr("Cancel"))
        cancel.setObjectName("FormButton")
        cancel.setCursor(Qt.PointingHandCursor)
        cancel.clicked.connect(self._cancel)
        h.addWidget(cancel)

    # -- public -------------------------------------------------------------
    def open_for(self):
        """Reset the fields and reveal the bar, focusing the name field."""
        self._name.clear()
        self._specific.setChecked(False)
        self.setVisible(True)
        self._name.setFocus()

    def close(self):
        self.setVisible(False)

    # -- internal -----------------------------------------------------------
    def _confirm(self):
        name = self._name.text().strip()
        if not name:
            self._name.setFocus()
            return
        specific = self._specific.isChecked()
        # Hide first so a validation-failure re-open (duplicate name) by the host
        # starts from a clean state - mirrors the Tk flow.
        self.setVisible(False)
        self.on_create(name, specific)

    def _cancel(self):
        self.setVisible(False)
        self.on_cancel()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Escape:
            self._cancel()
            return
        super().keyPressEvent(event)
