from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QCheckBox, QFormLayout, QHBoxLayout, QLabel,
                               QLineEdit, QListWidget, QListWidgetItem, QPushButton)

from gui_qt.dialogs.base import OverlayBase


class RepositoriesOverlay(OverlayBase):
    CARD_W = 760
    CARD_H = 570

    def __init__(self, host, store, *, title, hint, removed_message, normalize_source, on_done=None):
        super().__init__(host, on_done=on_done)
        self.store = store
        self.normalize_source = normalize_source
        self.removed_message = removed_message
        self.changed = False
        self.source_id = None
        card, layout = self._make_card("RepositoriesCard")
        title = QLabel(title, card)
        title.setStyleSheet("font-size:18px; font-weight:600;")
        layout.addWidget(title)
        hint = QLabel(hint, card)
        hint.setWordWrap(True)
        layout.addWidget(hint)
        self.list = QListWidget(card)
        self.list.currentItemChanged.connect(self._selection)
        layout.addWidget(self.list, 1)
        form = QFormLayout()
        self.url = QLineEdit(card)
        self.url.setPlaceholderText("https://github.com/owner/repository")
        self.branch = QLineEdit(card)
        self.branch.setPlaceholderText(self.tr("Default branch"))
        self.folder = QLineEdit(card)
        self.folder.setPlaceholderText(self.tr("Repository root"))
        form.addRow(self.tr("Repository URL"), self.url)
        form.addRow(self.tr("Branch"), self.branch)
        form.addRow(self.tr("Folder"), self.folder)
        self.enabled = QCheckBox(self.tr("Enable this repository"), card)
        form.addRow(self.enabled)
        layout.addLayout(form)
        self.status = QLabel(card)
        self.status.setTextFormat(Qt.PlainText)
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        buttons = self.buttons = QHBoxLayout()
        for label, callback in ((self.tr("New"), self._new), (self.tr("Save"), self._save)):
            button = QPushButton(label, card)
            button.clicked.connect(callback)
            buttons.addWidget(button)
        self.remove_button = QPushButton(self.tr("Remove"), card)
        self.remove_button.clicked.connect(self._remove)
        buttons.addWidget(self.remove_button)
        buttons.addStretch()
        close = QPushButton(self.tr("Close"), card)
        close.clicked.connect(self._finish)
        buttons.addWidget(close)
        layout.addLayout(buttons)
        self._reload()
        self._present()

    def _reload(self, selected=None):
        self.list.clear()
        try:
            for source in self.store.sources():
                suffix = self.tr("Enabled") if source["enabled"] else self.tr("Disabled")
                label = (f"{source['repository']} · {source['branch'] or self.tr('default')} · "
                         f"{source['folder'] or '/'} · {suffix}")
                if source["id"] == "builtin":
                    label += " · " + self.tr("Built-in")
                item = QListWidgetItem(label)
                item.setData(Qt.UserRole, source)
                self.list.addItem(item)
                if source["id"] == selected:
                    self.list.setCurrentItem(item)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            self.status.setText(str(exc))
        if self.list.currentItem() is None and self.list.count():
            self.list.setCurrentRow(0)

    def _selection(self, item, *_):
        source = item.data(Qt.UserRole) if item else {}
        self.source_id = source.get("id")
        self.url.setText(source.get("url", ""))
        self.branch.setText(source.get("branch", ""))
        self.folder.setText(source.get("folder", ""))
        self.enabled.setChecked(source.get("enabled", True))
        builtin = self.source_id == "builtin"
        for field in (self.url, self.branch, self.folder):
            field.setReadOnly(builtin)
        self.remove_button.setEnabled(bool(self.source_id) and not builtin)

    def _new(self):
        self.list.setCurrentRow(-1)
        self._selection(None)
        self.status.clear()
        self.url.setFocus()

    def _save(self):
        try:
            source = self.normalize_source(self.url.text(), self.branch.text(), self.folder.text(),
                                           self.enabled.isChecked())
            self.store.save_source(source["url"], source["branch"], source["folder"],
                                   enabled=source["enabled"], source_id=self.source_id)
            self.changed = True
            self._reload(source["id"])
            self.status.setText(self.tr("Repository saved."))
        except (OSError, ValueError, RuntimeError, KeyError, TypeError) as exc:
            self.status.setText(str(exc))

    def _remove(self):
        if not self.remove_button.isEnabled():
            return
        try:
            self.store.remove_source(self.source_id)
            self.changed = True
            self._reload()
            self.status.setText(self.removed_message)
        except (OSError, ValueError, RuntimeError, KeyError, TypeError) as exc:
            self.status.setText(str(exc))

    def _finish(self, result=None):
        super()._finish(self.changed)
