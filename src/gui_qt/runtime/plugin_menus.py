from __future__ import annotations

from amethyst_plugins.i18n import translate

import copy
from dataclasses import replace

from PySide6.QtCore import QEvent, QObject, QPoint, Qt, QTimer
from PySide6.QtGui import QContextMenuEvent
from PySide6.QtWidgets import QApplication, QLineEdit, QMenu, QPlainTextEdit, QTextEdit, QWidget

from amethyst_plugins.catalog import discover


def menu_item(kind, name="", path=None, **fields):
    item = {"type": kind, "name": str(name)}
    if path is not None:
        item["path"] = str(path)
    item.update({key: value for key, value in fields.items() if value is not None})
    return item


def add_plugin_actions(menu, source, location, clicked=None, selection=None, **details):
    app = QApplication.instance()
    controller = getattr(app, "_amethyst_plugin_menus", None)
    if controller is not None:
        try:
            controller.populate(menu, source, location, clicked, selection, details)
        except Exception as exc:
            controller.ui._report(f"Context menu {location}: {exc}")
    return menu


def bind_plugin_menu(menu, source, location, context):
    menu.aboutToShow.connect(
        lambda: add_plugin_actions(menu, source, location, **context()))


def matches(action, context):
    selection = context["selection"]
    count = len(selection)
    if (context["location"] not in action.locations or count < action.min_selection
            or action.max_selection is not None and count > action.max_selection):
        return False
    if action.item_types and (not selection or any(
            item["type"] not in action.item_types for item in selection)):
        return False
    if action.extensions and (not selection or any(
            item["type"] == "directory" or not str(item.get("path", item.get("name", ""))).lower().endswith(
                action.extensions) for item in selection)):
        return False
    return True


class PluginMenuController(QObject):
    def __init__(self, ui):
        super().__init__(ui)
        self.ui = ui
        self.locked_windows = []
        self.windows_locked = False
        app = QApplication.instance()
        app._amethyst_plugin_menus = self
        app.installEventFilter(self)

    def available(self):
        store = getattr(self.ui.host, "_plugin_store", None)
        return (not self.ui.stopped and not self.ui.locked and self.ui.valid_target()
                and getattr(self.ui.host, "_manager_plugin", None) is None
                and (store is None or not store.tab_close_blocked()))

    def set_locked(self, locked):
        if locked == self.windows_locked:
            return
        self.windows_locked = locked
        if locked:
            tabs = getattr(self.ui.host, "_tabs", None)
            for window in getattr(tabs, "_floats", ()):
                self.locked_windows.append((window, window.isEnabled()))
                window.setEnabled(False)
        else:
            for window, enabled in self.locked_windows:
                try:
                    window.setEnabled(enabled)
                except RuntimeError:
                    pass
            self.locked_windows.clear()

    def populate(self, menu, source, location, clicked, selection, details):
        previous = getattr(menu, "_plugin_actions_menu", None)
        if previous is not None:
            menu.removeAction(previous.menuAction())
            previous.deleteLater()
            menu._plugin_actions_menu = None
        separator = getattr(menu, "_plugin_actions_separator", None)
        if separator is not None:
            menu.removeAction(separator)
            separator.deleteLater()
            menu._plugin_actions_separator = None
        if not self.available() or source is not None and not source.isEnabled():
            return
        game = self.ui.host._gs.game
        source_game = getattr(source, "game", None) or getattr(source, "_game", None)
        if source_game is not None and getattr(source_game, "game_id", game.game_id) != game.game_id:
            return
        profile = self.ui.host._gs.profile
        source_profile = getattr(source, "profile_dir", None)
        if source_profile is not None and str(source_profile) != str(self.ui.host._gs.profile_dir()):
            return
        source_profile_name = getattr(source, "_profile_name", "")
        if source_profile_name and source_profile_name != profile:
            return
        context = copy.deepcopy({"location": location, "clicked": clicked,
                                 "selection": selection if selection is not None else (
                                     [clicked] if clicked is not None else []), **details})
        contributions = [(plugin, [a for a in plugin.context_menu_actions if matches(a, context)])
                         for plugin in self.ui.plugins.values()]
        path = (clicked or {}).get("path") if (clicked or {}).get("type") in ("file", "save", "game_plugin") else None
        previews = self.ui.previews.matching(path) if path else []
        commands = [(p, c) for p in self.ui.plugins.values() for c in p.commands
                    if c.context_menu and self.ui.commands.matches(c, context)]
        contributions = [(p, actions) for p, actions in contributions
                         if actions or any(other.id == p.id for other, _preview in previews)
                         or any(other.id == p.id for other, _command in commands)]
        if not contributions:
            return
        group = QMenu(self.tr("Plugins"), menu)
        group._plugin_submenus = []
        for plugin, actions in contributions:
            target = QMenu(translate(plugin, plugin.name).replace("&", "&&"), group)
            group._plugin_submenus.append(target)
            group.addMenu(target)
            for declaration in actions:
                action = target.addAction(translate(plugin, declaration.label).replace("&", "&&"))
                action.triggered.connect(
                    lambda _checked=False, p=plugin, a=declaration:
                    self.queue(menu, p, a, game.game_id, profile, context))
            for other, preview in previews:
                if other.id != plugin.id:
                    continue
                action = target.addAction(self.tr("Preview with {0}").format(translate(plugin, preview.label)).replace("&", "&&"))
                action.triggered.connect(
                    lambda _checked=False, p=plugin, d=preview:
                    self.ui.previews.queue(menu, path, (clicked or {}).get("relative_path", ""), p, d))
            for other, command in commands:
                if other.id != plugin.id:
                    continue
                action = target.addAction(translate(plugin, command.label).replace("&", "&&"))
                action.triggered.connect(
                    lambda _checked=False, p=plugin, c=command:
                    self.ui.commands.queue(p, c, menu=menu, target=(game.game_id, profile), context=context))
        if menu.actions() and not menu.actions()[-1].isSeparator():
            menu._plugin_actions_separator = menu.addSeparator()
        menu.addMenu(group)
        menu._plugin_actions_menu = group

    def queue(self, menu, plugin, action, game_id, profile, context):
        while isinstance(menu.parentWidget(), QMenu):
            menu = menu.parentWidget()
        menu.close()
        QTimer.singleShot(0, self, lambda: self.dispatch(plugin, action, game_id, profile, context))

    def dispatch(self, plugin, action, game_id, profile, context):
        if not self.available():
            return
        host = self.ui.host
        if host._gs.game.game_id != game_id or host._gs.profile != profile:
            host._notify(self.tr("The active game or profile changed. Reopen the menu."), "warning")
            return
        try:
            plugins, errors = discover(game_id)
            for error in errors:
                self.ui._report(error)
            current = next((p for p in plugins if p.id == plugin.id), None)
            if current != plugin or action not in current.context_menu_actions:
                raise ValueError(self.tr("The plugin changed. Reopen the menu."))
            script_plugin = replace(current, entrypoint=action.entrypoint)
            host._open_manager_plugin(script_plugin, action_id=action.id,
                                      menu_context=copy.deepcopy(context))
        except Exception as exc:
            self.ui._report(f"{plugin.id}/{action.id}: {exc}")
            host._notify(str(exc), "warning")

    def eventFilter(self, source, event):
        if event.type() != QEvent.ContextMenu or not self.available():
            return False
        if not any("text" in action.locations for plugin in self.ui.plugins.values()
                   for action in (*plugin.context_menu_actions, *(c.context for c in plugin.commands if c.context_menu))):
            return False
        editor = source
        if isinstance(source, QWidget) and isinstance(source.parentWidget(), (QTextEdit, QPlainTextEdit)):
            editor = source.parentWidget()
        if (not isinstance(editor, (QLineEdit, QTextEdit, QPlainTextEdit))
                or not editor.isEnabled() or editor.contextMenuPolicy() != Qt.DefaultContextMenu):
            return False
        try:
            if isinstance(editor, QLineEdit):
                selected = editor.selectedText() if editor.echoMode() == QLineEdit.Normal else ""
                menu = editor.createStandardContextMenu()
            else:
                selected = editor.textCursor().selectedText().replace("\u2029", "\n")
                pos = editor.viewport().mapFromGlobal(event.globalPos())
                menu = editor.createStandardContextMenu(pos)
            clicked = menu_item("text", editor.objectName(), text=selected,
                                read_only=editor.isReadOnly())
            path = editor.property("amethyst_context_path")
            if path:
                clicked["path"] = str(path)
            add_plugin_actions(menu, editor, "text", clicked,
                               [clicked] if selected else [], selected_text=selected)
            if getattr(menu, "_plugin_actions_menu", None) is None:
                menu.deleteLater()
                return False
            position = event.globalPos()
            if event.reason() == QContextMenuEvent.Keyboard and position == QPoint(-1, -1):
                position = editor.mapToGlobal(editor.rect().center())
            menu.exec(position)
            menu.deleteLater()
            return True
        except Exception as exc:
            self.ui._report(f"Text context menu: {exc}")
            return False

    def shutdown(self):
        self.set_locked(False)
        app = QApplication.instance()
        app.removeEventFilter(self)
        if getattr(app, "_amethyst_plugin_menus", None) is self:
            del app._amethyst_plugin_menus
