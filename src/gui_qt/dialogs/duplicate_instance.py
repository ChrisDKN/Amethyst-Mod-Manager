from PySide6.QtCore import Qt
from PySide6.QtWidgets import QHBoxLayout, QLabel, QLineEdit, QPushButton

from gui_qt.appearance.theme import active_palette, _c
from gui_qt.dialogs.base import OverlayBase
from Utils.games.instances import InstanceError, draft_instance, validate_name


class DuplicateInstanceOverlay(OverlayBase):
    CARD_W = 520
    CARD_H = 290
    CLICK_OUTSIDE_CANCELS = True

    def __init__(self, host, source, games, on_done):
        super().__init__(host, on_done=on_done)
        self._palette = active_palette()
        self._source, self._games = source, games
        _, layout = self._make_card("DuplicateInstanceCard")
        title = QLabel(self.tr("Duplicate Instance"))
        title.setStyleSheet(f"color:{_c(self._palette, 'TEXT_MAIN')};font-size:16px;font-weight:600;")
        layout.addWidget(title)
        description = QLabel(self.tr("Create a fresh instance using the same handler and download cache. Select its installation and launcher on the next screen."))
        description.setStyleSheet(f"color:{_c(self._palette, 'TEXT_DIM')};")
        description.setWordWrap(True)
        layout.addWidget(description)
        self._edit = QLineEdit()
        self._edit.setPlaceholderText(self.tr("New instance name"))
        self._edit.setMaxLength(100)
        layout.addWidget(self._edit)
        self._preview = QLabel()
        self._preview.setWordWrap(True)
        layout.addWidget(self._preview)
        layout.addStretch(1)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        cancel = QPushButton(self.tr("Cancel"))
        cancel.setObjectName("FormButton")
        cancel.setCursor(Qt.PointingHandCursor)
        cancel.clicked.connect(lambda: self._finish(None))
        buttons.addWidget(cancel)
        self._continue = QPushButton(self.tr("Continue"))
        self._continue.setObjectName("PrimaryButton")
        self._continue.setCursor(Qt.PointingHandCursor)
        self._continue.clicked.connect(self._confirm)
        buttons.addWidget(self._continue)
        layout.addLayout(buttons)
        self._edit.textChanged.connect(self._validate)
        self._edit.returnPressed.connect(self._confirm)
        self._validate()
        self._present()
        self._edit.setFocus()

    def _validate(self):
        try:
            _, key = validate_name(self._edit.text(), self._games)
            self._preview.setText(self.tr("Instance ID: {0}").format(key))
            self._preview.setStyleSheet(f"color:{_c(self._palette, 'TEXT_DIM')};")
            self._continue.setEnabled(True)
        except (InstanceError, OSError) as exc:
            self._preview.setText(str(exc))
            self._preview.setStyleSheet(f"color:{_c(self._palette, 'TEXT_ERR')};")
            self._continue.setEnabled(False)

    def _confirm(self):
        if not self._continue.isEnabled():
            return
        try:
            draft = draft_instance(self._source, self._edit.text(), self._games)
        except (InstanceError, OSError) as exc:
            self._preview.setText(str(exc))
            return
        self._finish(draft)
