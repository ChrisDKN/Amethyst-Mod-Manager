from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QHBoxLayout, QLabel, QLineEdit, QPushButton, QVBoxLayout, QWidget,
)

from Utils.bethesda.bodyslide import sanitize_output_name
from Utils.executables.launch import load_wizard_output_name, save_wizard_output_name


class BodySlideOutputWidget(QWidget):
    def __init__(self, game, tool: str, default: str, dim_style: str):
        super().__init__()
        self._game = game
        self._tool = tool
        self._builtin_default = default
        self._default = sanitize_output_name(
            load_wizard_output_name(game, tool), default)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 4, 0, 4)
        row = QHBoxLayout()
        row.setSpacing(8)
        row.addStretch(1)
        label = QLabel(self.tr("Output mod name:"))
        label.setStyleSheet(dim_style)
        row.addWidget(label)
        self._entry = QLineEdit()
        self._entry.setMinimumWidth(220)
        row.addWidget(self._entry)
        self._save_btn = QPushButton(self.tr("Save as default"))
        self._save_btn.setCursor(Qt.PointingHandCursor)
        self._save_btn.setToolTip(self.tr(
            "Remember this name for this tool and game, for both Linux and Proton."))
        self._save_btn.clicked.connect(self._save_default)
        row.addWidget(self._save_btn)
        self._reset_btn = QPushButton(self.tr("Reset default"))
        self._reset_btn.setCursor(Qt.PointingHandCursor)
        self._reset_btn.clicked.connect(self._reset_default)
        row.addWidget(self._reset_btn)
        row.addStretch(1)
        layout.addLayout(row)
        self._note = QLabel()
        self._note.setWordWrap(True)
        self._note.setAlignment(Qt.AlignCenter)
        self._note.setStyleSheet(dim_style)
        layout.addWidget(self._note)
        self._entry.textChanged.connect(self._update_save_button)
        self._refresh_default()

    def output_name(self) -> str:
        return sanitize_output_name(self._entry.text(), self._default)

    def _update_save_button(self):
        self._save_btn.setEnabled(bool(sanitize_output_name(self._entry.text(), "")))

    def _refresh_default(self):
        self._entry.setPlaceholderText(self._default)
        self._note.setText(self.tr(
            "Leave blank to use the default: {0}. Enter a name to override it for this run."
        ).format(self._default))
        self._reset_btn.setEnabled(self._default != self._builtin_default)
        self._update_save_button()

    def _save_default(self):
        name = self.output_name()
        if self._persist_default(name):
            self._entry.clear()

    def _reset_default(self):
        self._persist_default("")

    def _persist_default(self, name: str) -> bool:
        try:
            save_wizard_output_name(self._game, self._tool, name)
        except OSError as exc:
            self._note.setText(self.tr("Could not save the default output name: {0}").format(exc))
            return False
        self._default = name or self._builtin_default
        self._refresh_default()
        return True
