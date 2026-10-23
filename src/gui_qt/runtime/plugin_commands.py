from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from pathlib import Path

from PySide6.QtCore import QEvent, QObject, Qt, QTimer
from PySide6.QtWidgets import (QApplication, QHBoxLayout, QLabel, QLineEdit, QMenu,
                               QPushButton, QTreeWidget, QTreeWidgetItem, QWidget)

from amethyst_plugins.catalog import UIExtension, discover
from amethyst_plugins.i18n import translate
from gui_qt.dialogs.base import OverlayBase
from gui_qt.runtime.plugin_menus import matches
from Utils.ui.shortcuts import binding_identity, shortcut_from_parts


class CommandPalette(OverlayBase):
    CLICK_OUTSIDE_CANCELS = True

    def __init__(self, runtime):
        self.runtime = runtime
        self.entries = dict(runtime.entries)
        self.target = runtime.target()
        self.context = runtime.context()
        super().__init__(runtime.ui.host, on_done=lambda _result: runtime.closed(self),
                         card_w=760, card_h=600)
        card, layout = self._make_card("PluginCommandsCard")
        layout.addWidget(QLabel(self.tr("Plugin commands"), card))
        self.search = QLineEdit(card)
        self.search.setPlaceholderText(self.tr("Search commands…"))
        layout.addWidget(self.search)
        self.tree = QTreeWidget(card)
        self.tree.setHeaderLabels([self.tr("Command"), self.tr("Shortcut"), self.tr("Toolbar")])
        self.tree.setRootIsDecorated(False)
        self.tree.setColumnWidth(0, 410)
        for key, (plugin, command) in self.entries.items():
            item = QTreeWidgetItem([f"{translate(plugin, plugin.name)}: {translate(plugin, command.label)}", runtime.bindings.get(key, ""), ""])
            item.setData(0, Qt.UserRole, key)
            item.setCheckState(2, Qt.Checked if runtime.on_toolbar(plugin, command) else Qt.Unchecked)
            self.tree.addTopLevelItem(item)
        layout.addWidget(self.tree, 1)
        note = QLabel(self.tr("Assign shortcuts in Settings → Shortcuts. Select Toolbar to pin a command."), card)
        note.setWordWrap(True)
        layout.addWidget(note)
        buttons = QHBoxLayout()
        close = QPushButton(self.tr("Close"), card)
        self.run = QPushButton(self.tr("Run"), card)
        close.clicked.connect(self._finish)
        self.run.clicked.connect(self.execute)
        buttons.addStretch()
        buttons.addWidget(close)
        buttons.addWidget(self.run)
        layout.addLayout(buttons)
        self.tree.itemChanged.connect(self.pin)
        self.tree.currentItemChanged.connect(self.sync)
        self.tree.itemDoubleClicked.connect(lambda _item, column: self.execute() if column != 2 else None)
        self.search.textChanged.connect(self.filter)
        self.search.returnPressed.connect(self.execute)
        self.filter("")
        self._present()
        self.search.setFocus()

    def filter(self, text):
        first = None
        for index in range(self.tree.topLevelItemCount()):
            item = self.tree.topLevelItem(index)
            item.setHidden(text.casefold() not in item.text(0).casefold())
            if not item.isHidden() and first is None:
                first = item
        self.tree.setCurrentItem(first)
        self.sync()

    def sync(self, *_args):
        item = self.tree.currentItem()
        entry = self.entries.get(item.data(0, Qt.UserRole)) if item is not None else None
        self.run.setEnabled(bool(entry and self.runtime.matches(entry[1], self.context)))

    def pin(self, item, column):
        if column == 2:
            plugin, command = self.entries[item.data(0, Qt.UserRole)]
            self.runtime.pin(plugin, command, item.checkState(2) == Qt.Checked)

    def execute(self):
        item = self.tree.currentItem()
        if item is None or not self.run.isEnabled():
            return
        plugin, command = self.entries[item.data(0, Qt.UserRole)]
        self._finish()
        self.runtime.queue(plugin, command, target=self.target, context=self.context)


class PluginCommands(QObject):
    def __init__(self, ui):
        super().__init__(ui)
        self.ui = ui
        self.entries = {}
        self.bindings = {}
        self.palette = None
        QApplication.instance().installEventFilter(self)
        if not isinstance(ui.state.get("command_toolbar"), dict):
            ui.state["command_toolbar"] = {}

    @staticmethod
    def key(plugin, command):
        return f"plugin/{plugin.id}/{command.id}"

    def target(self):
        state = self.ui.host._gs
        return (state.game.game_id if state.game else "", state.profile)

    def context(self):
        self.ui.selections.flush()
        selection = self.ui.selections.selection()
        return {"location": selection["location"] or "modlist", "selection": selection["items"],
                "clicked": selection["current"], "source_id": selection["source_id"],
                "selection_count": selection["selection_count"], "truncated": selection["truncated"]}

    @staticmethod
    def matches(command, context):
        return not context.get("truncated", False) and matches(command.context, context)

    def update(self, plugins):
        entries = {self.key(p, c): (p, c) for p in plugins for c in p.commands}
        if entries == self.entries:
            return
        self.close()
        self.entries = entries
        from gui_qt.runtime.shortcuts import apply_shortcuts, current_shortcuts
        apply_shortcuts(self.ui.host)
        self.apply_bindings(current_shortcuts())

    def apply_bindings(self, values):
        self.bindings = {key: values.get(key, "") for key in self.entries}

    def on_toolbar(self, plugin, command):
        value = self.ui.state["command_toolbar"].get(self.key(plugin, command))
        if type(value) is bool:
            return value
        return command.toolbar or any(e.action == "run_command" and e.target == command.id
                                      and e.location == "toolbar" for e in plugin.ui_extensions)

    def pin(self, plugin, command, enabled):
        self.ui.state["command_toolbar"][self.key(plugin, command)] = enabled
        self.ui.save_timer.start()
        self.ui.schedule_refresh()

    def extensions(self, plugin):
        explicit = {e.target for e in plugin.ui_extensions if e.action == "run_command" and e.location == "toolbar"}
        return [UIExtension(f"command:{c.id}", c.label, "button", "toolbar", action="run_command", target=c.id)
                for c in plugin.commands if self.on_toolbar(plugin, c) and c.id not in explicit]

    def show(self):
        if self.palette is not None:
            self.palette.raise_()
        elif self.entries and self.ui.menus.available():
            self.palette = CommandPalette(self)

    def closed(self, palette):
        if self.palette is palette:
            self.palette = None

    def close(self):
        if self.palette is not None:
            self.palette._finish()

    def run(self, plugin, command_id, origin=None):
        if origin is not None and not origin.active:
            raise RuntimeError("This plugin context has been disposed.")
        entry = self.entries.get(f"plugin/{plugin.id}/{command_id}")
        if entry is None or entry[0] != plugin:
            raise ValueError("This command is no longer available.")
        if self.ui.popup is not None:
            self.ui.popup._finish()
        return self.queue(*entry, origin=origin)

    def queue(self, plugin, command, *, target=None, context=None, origin=None, menu=None):
        target = target or self.target()
        context = deepcopy(context) if context is not None else self.context()
        if not self.matches(command, context) or not self.ui.menus.available():
            return False
        if menu is not None:
            while isinstance(menu.parentWidget(), QMenu):
                menu = menu.parentWidget()
            menu.close()
        QTimer.singleShot(0, self, lambda: self.dispatch(plugin, command, target, context, origin))
        return True

    def dispatch(self, plugin, command, target, context, origin=None):
        if not self.ui.menus.available() or origin is not None and not origin.active:
            return
        host = self.ui.host
        try:
            if target != self.target():
                raise ValueError(self.tr("The active game or profile changed. Run the command again."))
            plugins, _errors = discover(target[0])
            current = next((p for p in plugins if p.id == plugin.id), None)
            if current != plugin or command not in current.commands:
                raise ValueError(self.tr("The plugin changed. Run the command again."))
            if not self.matches(command, context):
                raise ValueError(self.tr("This command does not match the current selection."))
            items = [*context["selection"], *([context["clicked"]] if context.get("clicked") else [])]
            for item in items:
                if context["location"] in ("modlist", "plugins"):
                    model = host._modlist_model if context["location"] == "modlist" else host._plugin_model
                    entries = model._entries if context["location"] == "modlist" else model._rows
                    if item["name"] not in {entry.name for entry in entries}:
                        raise ValueError(self.tr("A selected item is no longer available."))
                if item.get("path") and not Path(item["path"]).exists():
                    raise ValueError(self.tr("A selected item is no longer available."))
            context = dict(deepcopy(context), command_id=command.id)
            if command.action == "run_script":
                host._open_manager_plugin(replace(current, entrypoint=command.context.entrypoint),
                                          action_id=f"command:{command.id}", menu_context=context)
            elif command.action == "open_tab":
                self.ui.open_tab(f"{plugin.id}/{command.target}")
            else:
                host._open_manager_plugin(current)
        except Exception as exc:
            self.ui._report(f"Command {plugin.id}/{command.id}: {exc}")
            host._notify(str(exc), "warning")

    def eventFilter(self, source, event):
        if event.type() not in (QEvent.ShortcutOverride, QEvent.KeyPress, QEvent.MouseButtonPress):
            return False
        if not isinstance(source, QWidget) or not self.ui.menus.available():
            return False
        host = self.ui.host
        windows = (host, *getattr(getattr(host, "_tabs", None), "_floats", ()))
        if not any(source is window or window.isAncestorOf(source) for window in windows):
            return False
        from gui_qt.runtime.shortcuts import _EDITABLE_MODIFIERS, _focus_is_text_input, _overlay_open
        if not source.isEnabled() or _focus_is_text_input(host) or _overlay_open(host):
            return False
        modifiers = (event.modifiers() & _EDITABLE_MODIFIERS).value
        if event.type() == QEvent.MouseButtonPress:
            identity = ("mouse", modifiers, event.button().value)
        else:
            identity = binding_identity(shortcut_from_parts(modifiers, event.key()))
        if identity is None:
            return False
        for key, sequence in self.bindings.items():
            if binding_identity(sequence) != identity:
                continue
            plugin, command = self.entries[key]
            if event.type() == QEvent.MouseButtonPress:
                self.ui.selections.eventFilter(source, event)
            if not self.matches(command, self.context()):
                return False
            if event.type() != QEvent.MouseButtonPress and event.isAutoRepeat():
                return True
            if event.type() == QEvent.ShortcutOverride:
                event.accept()
                return True
            return self.queue(plugin, command)
        return False

    def shutdown(self):
        self.close()
        self.entries.clear()
        self.bindings.clear()
        QApplication.instance().removeEventFilter(self)
