"""Browse and install Winetricks components into the selected game's prefix."""

from __future__ import annotations

import threading
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QComboBox, QHeaderView, QHBoxLayout, QLabel, QLineEdit, QProgressBar,
    QPushButton, QTreeWidget, QTreeWidgetItem,
)

from gui_qt.dialogs.base import OverlayBase
from gui_qt.runtime.safe_emit import safe_emit
from gui_qt.appearance.theme import _c, active_palette, close_button
from Utils.wine.winetricks import list_windows_components, install_windows_component


class WindowsComponentsOverlay(OverlayBase):
    CARD_W = 900
    CARD_H = 600
    MIN_W = 460
    MIN_H = 340

    _loaded = Signal(object, str)

    def __init__(self, host, game, *, window):
        super().__init__(host)
        self._game = game
        self._prefix = Path(game.get_prefix_path())
        self._window = window
        self._installing = False
        self._buttons = {}
        self._loaded.connect(self._on_loaded)
        self._build()
        self._present()
        self._load()

    def _build(self):
        _, layout = self._make_card("WindowsComponentsCard")
        title = QLabel(self.tr("Install Windows component"))
        title.setStyleSheet(
            f"color:{_c(active_palette(), 'TEXT_MAIN')};"
            "font-weight:600; font-size:15px;")
        layout.addWidget(title)

        target = QLabel(self.tr("{0}\nPrefix: {1}").format(
            getattr(self._game, "name", ""), self._prefix))
        target.setTextFormat(Qt.PlainText)
        target.setTextInteractionFlags(Qt.TextSelectableByMouse)
        target.setWordWrap(True)
        layout.addWidget(target)

        filters = QHBoxLayout()
        self._search = QLineEdit()
        self._search.setPlaceholderText(self.tr("Search components…"))
        self._search.setClearButtonEnabled(True)
        self._search.textChanged.connect(self._filter)
        filters.addWidget(self._search, 1)
        self._category = QComboBox()
        self._category.addItem(self.tr("All components"), "")
        self._category.addItem(self.tr("DLLs and runtimes"), "dlls")
        self._category.addItem(self.tr("Fonts"), "fonts")
        self._category.currentIndexChanged.connect(self._filter)
        filters.addWidget(self._category)
        layout.addLayout(filters)

        self._list = QTreeWidget()
        self._list.setColumnCount(3)
        self._list.setHeaderLabels([
            self.tr("Component"), self.tr("Description"), self.tr("Install")])
        self._list.setRootIsDecorated(False)
        self._list.setAlternatingRowColors(True)
        self._list.setUniformRowHeights(True)
        self._list.header().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self._list.header().setSectionResizeMode(1, QHeaderView.Stretch)
        self._list.header().setStretchLastSection(False)
        self._list.header().setSectionResizeMode(2, QHeaderView.Fixed)
        self._list.setColumnWidth(2, 100)
        layout.addWidget(self._list, 1)

        self._status = QLabel()
        self._status.setTextFormat(Qt.PlainText)
        self._status.setWordWrap(True)
        layout.addWidget(self._status)
        self._bar = QProgressBar()
        self._bar.setRange(0, 0)
        self._bar.setTextVisible(False)
        self._bar.setFixedHeight(6)
        layout.addWidget(self._bar)

        buttons = QHBoxLayout()
        self._retry = QPushButton(self.tr("Retry"))
        self._retry.setObjectName("FormButton")
        self._retry.setCursor(Qt.PointingHandCursor)
        self._retry.clicked.connect(self._load)
        buttons.addWidget(self._retry)
        buttons.addStretch(1)
        self._close = close_button()
        self._close.clicked.connect(lambda: self._finish(None))
        buttons.addWidget(self._close)
        layout.addLayout(buttons)

    def _load(self):
        self._retry.hide()
        self._set_busy(True, self.tr("Loading Winetricks components…"))

        def worker():
            try:
                components = list_windows_components(self._window._append_log)
            except Exception as exc:
                self._window._append_log(f"Winetricks catalogue: {exc}")
                safe_emit(self._loaded, [], str(exc))
            else:
                safe_emit(self._loaded, components, "")

        threading.Thread(target=worker, daemon=True,
                         name="winetricks-components").start()

    def _on_loaded(self, components, error):
        if self._done:
            return
        self._set_busy(False, "")
        if error:
            self._status.setText(self.tr("Could not load components. See the log for details."))
            self._retry.show()
            return
        self._list.clear()
        self._buttons.clear()
        for component in components:
            item = QTreeWidgetItem([component.verb, component.description, ""])
            item.setData(0, Qt.UserRole, component)
            item.setToolTip(1, component.description)
            self._list.addTopLevelItem(item)
            button = QPushButton(self.tr("Install"))
            button.setObjectName("FormButton")
            button.setCursor(Qt.PointingHandCursor)
            button.setAccessibleName(self.tr("Install {0}").format(component.verb))
            button.clicked.connect(lambda _=False, c=component: self._install(c))
            self._list.setItemWidget(item, 2, button)
            self._buttons[component.verb] = button
        self._filter()
        self._search.setFocus()

    def _filter(self):
        words = self._search.text().casefold().split()
        category = self._category.currentData()
        visible = 0
        for index in range(self._list.topLevelItemCount()):
            item = self._list.topLevelItem(index)
            component = item.data(0, Qt.UserRole)
            text = f"{component.verb} {component.description}".casefold()
            matches = (not category or component.category == category) and all(
                word in text for word in words)
            item.setHidden(not matches)
            visible += matches
        self._status.setText(self.tr("{0} components").format(visible) if visible
                             else self.tr("No matching components."))

    def _set_busy(self, busy, message):
        self._list.setEnabled(not busy)
        self._search.setEnabled(not busy)
        self._category.setEnabled(not busy)
        self._close.setEnabled(not self._installing)
        self._bar.setVisible(busy)
        self._status.setText(message)

    def _install(self, component):
        if self._done or self._installing:
            return
        self._installing = True
        self._set_busy(True, self.tr("Installing {0}… (details in the log)")
                       .format(component.verb))
        started = self._window._run_proton_installer(
            self.tr("Installing {0}").format(component.verb),
            lambda log: install_windows_component(
                self._prefix, component, game=self._game, log_fn=log),
            on_done=lambda ok: self._on_installed(component, ok))
        if not started:
            self._installing = False
            self._set_busy(False, self.tr("Installer could not start. See the log for details."))

    def _on_installed(self, component, ok):
        if self._done:
            return
        self._installing = False
        message = (self.tr("{0} installed successfully.") if ok else
                   self.tr("{0} failed to install. See the log for details."))
        self._set_busy(False, message.format(component.verb))
        if ok:
            button = self._buttons[component.verb]
            button.setText(self.tr("Installed"))
            button.setEnabled(False)

    def _finish(self, result=None):
        if not self._installing:
            super()._finish(result)
