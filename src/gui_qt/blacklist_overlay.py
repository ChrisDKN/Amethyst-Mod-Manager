"""Per-game filename and folder blacklist editor."""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView, QHBoxLayout, QLabel, QLineEdit, QPushButton,
    QTabWidget, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

from Utils.games.conflict_blacklist import (
    BlacklistOverrides, builtin_rules, normalize_pattern,
)
from gui_qt.overlay_base import OverlayBase
from gui_qt.theme_qt import active_palette, _c, close_button


class BlacklistOverlay(OverlayBase):
    CARD_W = 660
    CARD_H = 540
    MIN_W = 360
    MIN_H = 340

    def __init__(self, host, game, initial: BlacklistOverrides, on_save):
        super().__init__(host)
        self._on_save = on_save
        files, folders = builtin_rules(game)
        self._defaults = {"files": files, "folders": folders}
        self._custom = {kind: set(getattr(initial, kind))
                        for kind in self._defaults}
        self._disabled = {kind: set(getattr(initial, "disabled_" + kind))
                          for kind in self._defaults}
        self._lists = {}
        self._inputs = {}
        self._actions = {}
        p = active_palette()
        _card, layout = self._make_card("_BlacklistCard")
        header = QHBoxLayout()
        title = QLabel(self.tr("Blacklist — {0}").format(game.name))
        title.setWordWrap(True)
        title.setStyleSheet(f"color:{_c(p, 'TEXT_MAIN')}; font-weight:600;")
        header.addWidget(title, 1)
        close = close_button(pal=p)
        close.clicked.connect(lambda: self._finish(None))
        header.addWidget(close)
        layout.addLayout(header)

        help_text = QLabel(self.tr(
            "Applies to all profiles for this game. Matching files and folders "
            "are excluded from deployment and conflict tracking; files remain "
            "in staging. Changes apply on the next deploy, or automatically "
            "when auto-deploy is enabled."))
        help_text.setWordWrap(True)
        layout.addWidget(help_text)
        tabs = QTabWidget()
        layout.addWidget(tabs, 1)
        for kind, label, hint in (
            ("files", self.tr("Files"), self.tr(
                "Case-insensitive filename patterns, e.g. *.txt or readme*.")),
            ("folders", self.tr("Folders"), self.tr(
                "Case-insensitive folder-name patterns at any depth, e.g. docs "
                "or *_backup. Matching folders exclude their entire contents.")),
        ):
            page = QWidget()
            body = QVBoxLayout(page)
            description = QLabel(hint)
            description.setWordWrap(True)
            body.addWidget(description)
            values = QTreeWidget()
            values.setColumnCount(3)
            values.setHeaderLabels([self.tr("Pattern"), self.tr("Source"), self.tr("Status")])
            values.setRootIsDecorated(False)
            values.setSelectionMode(QAbstractItemView.SingleSelection)
            values.setColumnWidth(0, 270)
            values.setColumnWidth(1, 100)
            body.addWidget(values, 1)
            self._lists[kind] = values

            row = QHBoxLayout()
            edit = QLineEdit()
            edit.setPlaceholderText(self.tr("Add a pattern…"))
            edit.setClearButtonEnabled(True)
            self._inputs[kind] = edit
            row.addWidget(edit, 1)
            add = self._button(self.tr("Add"), lambda _checked=False, k=kind: self._add(k))
            row.addWidget(add)
            edit.returnPressed.connect(lambda k=kind: self._add(k))
            body.addLayout(row)
            action = self._button(self.tr("Remove"), lambda _checked=False, k=kind: self._remove_or_restore(k))
            self._actions[kind] = action
            body.addWidget(action, 0, Qt.AlignRight)
            values.itemSelectionChanged.connect(lambda k=kind: self._sync_action(k))
            tabs.addTab(page, label)

        self._error = QLabel()
        self._error.setWordWrap(True)
        self._error.hide()
        layout.addWidget(self._error)
        footer = QHBoxLayout()
        footer.addWidget(self._button(self.tr("Restore built-ins"), self._restore_builtins))
        footer.addStretch(1)
        footer.addWidget(self._button(self.tr("Cancel"), lambda: self._finish(None)))
        save = self._button(self.tr("Save"), self._save)
        save.setObjectName("PrimaryButton")
        footer.addWidget(save)
        layout.addLayout(footer)
        for kind in self._lists:
            self._populate(kind)
        self._present()
        self._inputs["files"].setFocus()

    @staticmethod
    def _button(label, callback):
        button = QPushButton(label)
        button.setObjectName("FormButton")
        button.setCursor(Qt.PointingHandCursor)
        button.clicked.connect(callback)
        return button

    def _populate(self, kind, selected=None):
        values = self._lists[kind]
        values.clear()
        defaults = self._defaults[kind]
        for pattern in sorted(defaults | self._custom[kind]):
            builtin = pattern in defaults
            disabled = (builtin and pattern in self._disabled[kind]
                        and pattern not in self._custom[kind])
            item = QTreeWidgetItem([
                pattern, self.tr("Built-in") if builtin else self.tr("Custom"),
                self.tr("Disabled") if disabled else self.tr("Active"),
            ])
            item.setData(0, Qt.UserRole, pattern)
            if disabled:
                font = item.font(0)
                font.setStrikeOut(True)
                item.setFont(0, font)
            values.addTopLevelItem(item)
            if pattern == selected:
                values.setCurrentItem(item)
        self._sync_action(kind)

    def _sync_action(self, kind):
        item = self._lists[kind].currentItem()
        disabled = (item is not None and item.data(0, Qt.UserRole)
                    in (self._disabled[kind] & self._defaults[kind]) - self._custom[kind])
        self._actions[kind].setEnabled(item is not None)
        self._actions[kind].setText(self.tr("Restore") if disabled else self.tr("Remove"))

    def _add(self, kind):
        try:
            pattern = normalize_pattern(self._inputs[kind].text())
        except ValueError:
            self._error.setText(self.tr("Enter a filename or folder-name pattern without path separators."))
            self._error.show()
            return
        self._error.hide()
        if pattern in self._defaults[kind]:
            self._disabled[kind].discard(pattern)
            self._custom[kind].discard(pattern)
        else:
            self._custom[kind].add(pattern)
        self._inputs[kind].clear()
        self._populate(kind, pattern)

    def _remove_or_restore(self, kind):
        item = self._lists[kind].currentItem()
        if item is None:
            return
        pattern = item.data(0, Qt.UserRole)
        disabled = pattern in self._disabled[kind] and pattern not in self._custom[kind]
        self._custom[kind].discard(pattern)
        if pattern in self._defaults[kind]:
            if disabled:
                self._disabled[kind].remove(pattern)
            else:
                self._disabled[kind].add(pattern)
        self._error.hide()
        self._populate(kind, pattern)

    def _restore_builtins(self):
        for kind in self._lists:
            self._disabled[kind].clear()
            self._populate(kind)
        self._error.hide()

    def _save(self):
        if self._done:
            return
        overrides = BlacklistOverrides(
            **{kind: frozenset(patterns) for kind, patterns in self._custom.items()},
            **{"disabled_" + kind: frozenset(patterns)
               for kind, patterns in self._disabled.items()},
        )
        try:
            self._on_save(overrides)
        except Exception as exc:
            self._error.setText(self.tr("Could not save blacklist: {0}").format(exc))
            self._error.show()
            return
        self._finish(overrides)
