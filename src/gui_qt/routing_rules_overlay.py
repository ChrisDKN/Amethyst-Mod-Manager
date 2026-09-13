"""Ordered routing rule editor for the selected game."""

import copy

from PySide6.QtCore import Qt, QSignalBlocker
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QFormLayout, QHBoxLayout, QLabel,
    QLineEdit, QPlainTextEdit, QPushButton, QScrollArea, QSplitter,
    QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

from Utils.deployment.shared import CustomRule
from Utils.games.routing_rules import new_entry, rule_values, validate_rule
from gui_qt.overlay_base import OverlayBase
from gui_qt.theme_qt import active_palette, _c, close_button


class RoutingRulesOverlay(OverlayBase):
    CARD_W = 900
    CARD_H = 720
    MIN_W = 420
    MIN_H = 400

    def __init__(self, host, game, initial, on_save):
        super().__init__(host)
        self._entries = copy.deepcopy(initial)
        self._on_save = on_save
        self._selected = -1
        self._fields = {}
        self._loaded_values = None
        p = active_palette()
        _card, layout = self._make_card("_RoutingRulesCard")
        header = QHBoxLayout()
        title = QLabel(self.tr("Routing Rules — {0}").format(game.name))
        title.setWordWrap(True)
        title.setStyleSheet(f"color:{_c(p, 'TEXT_MAIN')}; font-weight:600;")
        header.addWidget(title, 1)
        close = close_button(pal=p)
        close.clicked.connect(lambda: self._finish(None))
        header.addWidget(close)
        layout.addLayout(header)
        help_text = QLabel(self.tr(
            "Applies to all profiles for this game. Earlier matching rules take "
            "precedence. Changes apply on the next deploy, or automatically when "
            "auto-deploy is enabled. Blacklist exclusions still apply. "
            "Removed built-ins remain available to restore."))
        help_text.setWordWrap(True)
        layout.addWidget(help_text)

        split = QSplitter(Qt.Vertical)
        layout.addWidget(split, 1)
        list_page = QWidget()
        list_layout = QVBoxLayout(list_page)
        list_layout.setContentsMargins(0, 0, 0, 0)
        self._list = QTreeWidget()
        self._list.setHeaderLabels([
            self.tr("Match"), self.tr("Destination"), self.tr("Source"), self.tr("Status")])
        self._list.setRootIsDecorated(False)
        self._list.setSelectionMode(QAbstractItemView.SingleSelection)
        self._list.setColumnWidth(0, 280)
        self._list.setColumnWidth(1, 230)
        self._list.setColumnWidth(2, 130)
        self._list.currentItemChanged.connect(self._selection_changed)
        self._list.itemDoubleClicked.connect(lambda *_: self._edit())
        list_layout.addWidget(self._list)
        actions = QHBoxLayout()
        actions.addWidget(self._button(self.tr("Add"), self._add))
        self._edit_button = self._button(self.tr("Edit"), self._edit)
        actions.addWidget(self._edit_button)
        self._up = self._button(self.tr("Move up"), lambda: self._move(-1))
        self._down = self._button(self.tr("Move down"), lambda: self._move(1))
        actions.addWidget(self._up)
        actions.addWidget(self._down)
        self._remove = self._button(self.tr("Remove"), self._remove_or_restore)
        actions.addWidget(self._remove)
        list_layout.addLayout(actions)
        split.addWidget(list_page)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        self._form_page = QWidget()
        form = QFormLayout(self._form_page)
        form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
        form.setRowWrapPolicy(QFormLayout.WrapLongRows)
        destination = QLineEdit()
        destination.setPlaceholderText(self.tr("Empty = root of the selected destination base"))
        self._fields["dest"] = destination
        form.addRow(self.tr("Destination"), destination)
        for key, label, hint in (
            ("extensions", self.tr("Extensions"), self.tr("One per line, e.g. .pak. Combined with folders when both are set.")),
            ("folders", self.tr("Folders"), self.tr("One folder name or relative path per line. Matching is case-insensitive; spelling controls destination casing.")),
            ("filenames", self.tr("Filenames"), self.tr("One filename pattern per line, e.g. loader*.dll. Filename matches are also accepted when other criteria are set.")),
            ("companion_extensions", self.tr("Companion extensions"), self.tr("One extension per line. Route same-stem siblings alongside matching files.")),
            ("exclude_extensions", self.tr("Excluded extensions"), self.tr("One extension per line. These files cannot match this rule.")),
            ("mirror_dests", self.tr("Mirrored destinations"), self.tr("One additional relative destination per line, under the same base.")),
        ):
            edit = QPlainTextEdit()
            edit.setFixedHeight(58)
            edit.setPlaceholderText(hint)
            edit.setToolTip(hint)
            self._fields[key] = edit
            form.addRow(label, edit)
        for key, label, hint in (
            ("loose_only", self.tr("Loose only"), self.tr("Match loose files or a matching folder at the mod root.")),
            ("flatten", self.tr("Flatten"), self.tr("For folder matches, remove leading folders above the match. For filename or extension matches, use the bare filename.")),
            ("include_siblings", self.tr("Include siblings"), self.tr("Bring the containing folder's contents along, keeping the folder name. Overrides Flatten for these matches.")),
            ("to_prefix", self.tr("To Prefix"), self.tr("Destinations are relative to the Proton/Wine prefix root instead of the game root. Requires a configured prefix.")),
        ):
            check = QCheckBox(label)
            check.setToolTip(hint)
            self._fields[key] = check
            form.addRow(check)
        self._reset = self._button(self.tr("Reset to built-in"), self._reset_selected)
        form.addRow(self._reset)
        scroll.setWidget(self._form_page)
        split.addWidget(scroll)
        split.setSizes([260, 280])

        self._error = QLabel()
        self._error.setTextFormat(Qt.PlainText)
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
        self._populate(0 if self._entries else -1)
        self._present()

    @staticmethod
    def _button(label, callback):
        button = QPushButton(label)
        button.setObjectName("FormButton")
        button.setCursor(Qt.PointingHandCursor)
        button.clicked.connect(callback)
        return button

    def _populate(self, selected):
        with QSignalBlocker(self._list):
            self._list.clear()
            for entry in self._entries:
                rule = entry.rule
                match = []
                for key, label in (("extensions", self.tr("Extensions")),
                                   ("folders", self.tr("Folders")),
                                   ("filenames", self.tr("Filenames"))):
                    if getattr(rule, key):
                        match.append(label + ": " + ", ".join(getattr(rule, key)))
                base = self.tr("Prefix") if rule.to_prefix else self.tr("Game")
                source = self.tr("Custom")
                if entry.builtin:
                    source = self.tr("Built-in (edited)") if entry.edits else self.tr("Built-in")
                item = QTreeWidgetItem([
                    "; ".join(match) or self.tr("New rule"),
                    base + ": " + (rule.dest or self.tr("Root")), source,
                    self.tr("Disabled") if entry.disabled else self.tr("Active")])
                for column in range(4):
                    item.setToolTip(column, item.text(column))
                    if entry.disabled:
                        font = item.font(column)
                        font.setStrikeOut(True)
                        item.setFont(column, font)
                self._list.addTopLevelItem(item)
            if 0 <= selected < len(self._entries):
                self._list.setCurrentItem(self._list.topLevelItem(selected))
        self._load_form(selected)

    def _load_form(self, index):
        self._selected = index
        valid = 0 <= index < len(self._entries)
        entry = self._entries[index] if valid else None
        values = rule_values(entry.rule if entry else CustomRule(dest=""))
        for key, widget in self._fields.items():
            if isinstance(widget, QCheckBox):
                widget.setChecked(values[key])
            elif isinstance(widget, QPlainTextEdit):
                widget.setPlainText("\n".join(values[key]))
            else:
                widget.setText(values[key])
        self._loaded_values = self._form_values()
        self._form_page.setEnabled(valid)
        self._edit_button.setEnabled(valid)
        self._remove.setEnabled(valid)
        self._remove.setText(self.tr("Restore") if entry and entry.disabled else self.tr("Remove"))
        self._up.setEnabled(valid and index > 0)
        self._down.setEnabled(valid and index < len(self._entries) - 1)
        self._reset.setEnabled(bool(entry and entry.builtin))

    def _form_values(self):
        values = {}
        for key, widget in self._fields.items():
            if isinstance(widget, QCheckBox):
                values[key] = widget.isChecked()
            elif isinstance(widget, QPlainTextEdit):
                values[key] = [line.strip() for line in widget.toPlainText().splitlines() if line.strip()]
            else:
                values[key] = widget.text()
        return values

    def _store_form(self):
        if self._selected < 0:
            return True
        try:
            values = self._form_values()
            rule = validate_rule(values)
            if values != self._loaded_values:
                self._entries[self._selected].edit(rule)
            self._error.hide()
            return True
        except ValueError as exc:
            self._error.setText(str(exc))
            self._error.show()
            return False

    def _selection_changed(self, current, _previous):
        if not self._store_form():
            with QSignalBlocker(self._list):
                self._list.setCurrentItem(self._list.topLevelItem(self._selected))
            return
        self._populate(self._list.indexOfTopLevelItem(current))

    def _edit(self):
        if self._selected >= 0:
            self._fields["dest"].setFocus()

    def _add(self):
        if self._store_form():
            self._entries.append(new_entry(CustomRule(dest="")))
            self._populate(len(self._entries) - 1)
            self._fields["filenames"].setFocus()

    def _move(self, delta):
        index = self._selected
        target = index + delta
        if index < 0 or not 0 <= target < len(self._entries) or not self._store_form():
            return
        self._entries[index].moved = self._entries[target].moved = True
        self._entries[index], self._entries[target] = self._entries[target], self._entries[index]
        self._populate(target)

    def _remove_or_restore(self):
        if self._selected < 0:
            return
        entry = self._entries[self._selected]
        if entry.builtin or entry.disabled:
            if not self._store_form():
                return
            entry.disabled = not entry.disabled
        else:
            self._entries.pop(self._selected)
        self._error.hide()
        self._populate(min(self._selected, len(self._entries) - 1))

    def _restore_builtins(self):
        if not self._store_form():
            return
        for entry in self._entries:
            if entry.builtin:
                entry.disabled = False
        self._populate(self._selected)

    def _reset_selected(self):
        if self._selected >= 0 and self._entries[self._selected].builtin:
            self._entries[self._selected].edits.clear()
            self._error.hide()
            self._populate(self._selected)

    def _save(self):
        if self._done or not self._store_form():
            return
        try:
            self._on_save(copy.deepcopy(self._entries))
        except Exception as exc:
            self._error.setText(self.tr("Could not save routing rules: {0}").format(exc))
            self._error.show()
            return
        self._finish(self._entries)
