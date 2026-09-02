"""In-window overlay shown when installing a mod whose folder already exists.
Qt equivalent of the Tk ``_ReplaceModDialog`` - a dimmed child overlay (see
gui_qt/overlay_base.py).

`on_done(result)` is called with:
    "replace"        - wipe the existing folder + reinstall (keep its position)
    "rename:<name>"  - install as a NEW mod under <name>
    "cancel"         - abort the install
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QWidget, QHBoxLayout, QLabel, QLineEdit, QMenu, QPushButton,
)

from gui_qt.overlay_base import OverlayBase
from gui_qt.theme_qt import active_palette, _c


class ModExistsOverlay(OverlayBase):
    CARD_W = 460
    CARD_H = 240
    MIN_W = 320
    MIN_H = 180
    ESC_RESULT = "cancel"

    def __init__(self, host: QWidget, mod_name: str, conflict: bool, on_done,
                 suggestions=None):
        super().__init__(host, on_done=on_done)
        p = active_palette()
        from gui_qt.text_input_overlay import normalise_suggestions
        self._suggestions = normalise_suggestions(suggestions)

        _card, v = self._make_card("ExistsCard")

        title = QLabel(self.tr("Mod Already Exists"))
        title.setStyleSheet(
            f"color:{_c(p,'TEXT_MAIN')}; font-weight:600; font-size:16px;")
        v.addWidget(title)

        if conflict:
            body_text = self.tr(
                "'{0}' is also already installed.\n"
                "Pick a different name, or choose another option.").format(mod_name)
        else:
            body_text = self.tr(
                "'{0}' is already installed.\n"
                "How would you like to handle the existing mod?").format(mod_name)
        body = QLabel(body_text)
        body.setStyleSheet(f"color:{_c(p,'TEXT_DIM')}; font-size:13px;")
        body.setWordWrap(True)
        v.addWidget(body)

        # Inline rename field (hidden until "Rename…" is pressed).
        self._rename_row = QWidget()
        rr = QHBoxLayout(self._rename_row)
        rr.setContentsMargins(0, 0, 0, 0); rr.setSpacing(6)
        self._entry = QLineEdit()
        self._entry.setPlaceholderText(self.tr("New mod name…"))
        self._entry.setText(mod_name)
        self._entry.returnPressed.connect(self._confirm_rename)
        rr.addWidget(self._entry, 1)
        if self._suggestions:
            from gui_qt.text_input_overlay import make_suggestion_button
            rr.addWidget(make_suggestion_button(self._show_suggestions))
        confirm = QPushButton(self.tr("OK"))
        confirm.setObjectName("PrimaryButton")
        confirm.setCursor(Qt.PointingHandCursor)
        confirm.clicked.connect(self._confirm_rename)
        rr.addWidget(confirm)
        self._rename_row.setVisible(False)
        v.addWidget(self._rename_row)

        v.addStretch(1)

        bar = QHBoxLayout()
        bar.addStretch(1)
        cancel = QPushButton(self.tr("Cancel"))
        cancel.setObjectName("FormButton")       # neutral, like other buttons
        cancel.setCursor(Qt.PointingHandCursor)
        cancel.clicked.connect(lambda: self._finish("cancel"))
        bar.addWidget(cancel)
        rename = QPushButton(self.tr("Rename…"))
        rename.setObjectName("FormButton")
        rename.setCursor(Qt.PointingHandCursor)
        rename.clicked.connect(self._show_rename)
        bar.addWidget(rename)
        replace = QPushButton(self.tr("Replace All"))
        replace.setObjectName("PrimaryButton")   # accent primary action
        replace.setCursor(Qt.PointingHandCursor)
        replace.clicked.connect(lambda: self._finish("replace"))
        bar.addWidget(replace)
        v.addLayout(bar)

        self._present()

    @classmethod
    def show_over(cls, host, mod_name, conflict, on_done, suggestions=None):
        top = host.window() if host is not None else None
        return cls(top or host, mod_name, conflict, on_done, suggestions)

    # -- internals ----------------------------------------------------------
    def _show_rename(self):
        self._rename_row.setVisible(True)
        self._entry.setFocus()
        self._entry.selectAll()

    def _show_suggestions(self, anchor):
        from gui_qt.text_input_overlay import fill_suggestion_menu
        menu = QMenu(self)
        fill_suggestion_menu(menu, self._suggestions, self._apply_suggestion)
        menu.exec(anchor.mapToGlobal(anchor.rect().bottomLeft()))

    def _apply_suggestion(self, text: str):
        self._entry.setText(text)
        self._entry.setFocus()
        self._entry.selectAll()

    def _confirm_rename(self):
        from Utils.mods.names import sanitize_mod_folder_name
        name = sanitize_mod_folder_name(self._entry.text().strip())
        if not name:
            return
        self._finish(f"rename:{name}")
