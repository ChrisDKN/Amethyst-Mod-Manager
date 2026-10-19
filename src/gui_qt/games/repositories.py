from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QComboBox, QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QPushButton

from gui_qt.dialogs.base import OverlayBase
from gui_qt.dialogs.repositories import RepositoriesOverlay


class GameRepositoriesOverlay(RepositoriesOverlay):
    def __init__(self, host, store, on_sources, on_done=None):
        from Utils.github.handler_sources import normalize_source
        super().__init__(
            host, store, on_done=on_done, normalize_source=normalize_source,
            title=self.tr("Custom game repositories"),
            hint=self.tr("Choose a folder containing custom game handler JSON files. "
                         "Blank branch uses the default branch; blank folder uses the repository root. "
                         "Close to sync changes. Use Game sources to choose between handlers with the same game ID. "
                         "Disabling or removing a repository keeps downloaded games."),
            removed_message=self.tr("Repository removed. Downloaded games are kept."))
        sources = QPushButton(self.tr("Game sources…"), self._card)
        sources.clicked.connect(on_sources)
        self.buttons.insertWidget(3, sources)


class GameSourcesOverlay(OverlayBase):
    CARD_W = 820
    CARD_H = 560

    def __init__(self, host, store, on_done=None):
        super().__init__(host, on_done=on_done)
        self.store = store
        self.changed = False
        card, layout = self._make_card("GameSourcesCard")
        title = QLabel(self.tr("Game handler sources"), card)
        title.setStyleSheet("font-size:18px; font-weight:600;")
        layout.addWidget(title)
        hint = QLabel(self.tr("Choose one handler source for each game ID. The game's existing name is kept "
                              "so saved paths and profiles continue to work. Other downloaded versions are kept. "
                              "Disabled or removed repositories keep their last downloaded version."), card)
        hint.setWordWrap(True)
        layout.addWidget(hint)
        self.list = QListWidget(card)
        self.list.currentItemChanged.connect(self._selection)
        layout.addWidget(self.list, 1)
        self.sources = QComboBox(card)
        self.sources.setMinimumContentsLength(30)
        self.sources.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        layout.addWidget(self.sources)
        self.status = QLabel(card)
        self.status.setTextFormat(Qt.PlainText)
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        buttons = QHBoxLayout()
        self.use_button = QPushButton(self.tr("Use selected source"), card)
        self.use_button.clicked.connect(self._use_source)
        buttons.addWidget(self.use_button)
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
            games = self.store.games()
            for game in sorted(games, key=lambda game: (len(game["options"]) == 1, game["game_id"].lower())):
                label = f"{game['name']} · {game['game_id']}"
                if len(game["options"]) > 1:
                    label += " · " + self.tr("{0} sources").format(len(game["options"]))
                item = QListWidgetItem(label)
                item.setData(Qt.UserRole, game)
                self.list.addItem(item)
                if game["game_id"] == selected:
                    self.list.setCurrentItem(item)
            if not games:
                self.status.setText(self.tr("No downloaded handlers. Close the repositories window to sync first."))
        except (OSError, ValueError, KeyError, TypeError) as exc:
            self.status.setText(str(exc))
        if self.list.currentItem() is None and self.list.count():
            self.list.setCurrentRow(0)
        self.use_button.setEnabled(bool(self.list.count()))

    def _selection(self, item, *_):
        self.sources.clear()
        if item is None:
            return
        game = item.data(Qt.UserRole)
        enabled = {source["id"] for source in self.store.sources() if source["enabled"]}
        for option in game["options"]:
            if option["source_id"] is None:
                label = self.tr("Local definition") + " · " + option["filename"]
            else:
                label = (f"{option.get('repository', option['source_id'])} · "
                         f"{option.get('branch') or self.tr('default')} · "
                         f"{option.get('folder') or '/'} · {option['filename']}")
                if option["source_id"] not in enabled:
                    label += " · " + self.tr("Retained download; updates disabled")
            label += " · " + option["definition"]["name"]
            self.sources.addItem(label, option["key"])
            if option["key"] == game["selected"]:
                self.sources.setCurrentIndex(self.sources.count() - 1)

    def _use_source(self):
        item = self.list.currentItem()
        if item is None:
            return
        game = item.data(Qt.UserRole)
        try:
            self.changed = self.store.select(game["game_id"], self.sources.currentData()) or self.changed
            self._reload(game["game_id"])
            self.status.setText(self.tr("Handler source selected."))
        except (OSError, ValueError, RuntimeError, KeyError, TypeError) as exc:
            self.status.setText(str(exc))

    def _finish(self, result=None):
        super()._finish(self.changed)
