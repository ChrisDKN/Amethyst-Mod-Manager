from __future__ import annotations

import importlib.util
import json
import sys
import uuid
from dataclasses import dataclass

from PySide6.QtCore import QEvent, QObject, QSize, Qt, QTimer
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import (QApplication, QBoxLayout, QMenu, QPushButton,
                               QScrollArea, QSizePolicy, QSplitter, QTabWidget,
                               QToolButton, QVBoxLayout, QWidget)

from amethyst_plugins.catalog import (UIExtension, data_path, discover,
                                      load_data, load_settings, plugin_directory, settings_path)
from amethyst_plugins.i18n import translate
from gui_qt.appearance.icons import icon
from gui_qt.appearance.theme import bind_theme
from gui_qt.dialogs.base import OverlayBase


class PluginPanelArea(QWidget):
    def __init__(self, body):
        super().__init__()
        self.right = QTabWidget()
        self.bottom = QTabWidget()
        for tabs in (self.right, self.bottom):
            tabs.setTabsClosable(True)
            tabs.setMinimumSize(0, 0)
            tabs.hide()
        self.horizontal = QSplitter(Qt.Horizontal)
        self.horizontal.addWidget(body)
        self.horizontal.addWidget(self.right)
        self.horizontal.setStretchFactor(0, 1)
        self.horizontal.setStretchFactor(1, 0)
        self.vertical = QSplitter(Qt.Vertical)
        self.vertical.addWidget(self.horizontal)
        self.vertical.addWidget(self.bottom)
        self.vertical.setStretchFactor(0, 1)
        self.vertical.setStretchFactor(1, 0)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self.vertical)


class _Module:
    def __init__(self, plugin):
        self.name = f"_amethyst_plugin_ui_{uuid.uuid4().hex}"
        self.users = 0
        self.retired = False
        spec = importlib.util.spec_from_file_location(
            self.name, plugin.entrypoint,
            submodule_search_locations=[str(plugin.entrypoint.parent)])
        self.module = importlib.util.module_from_spec(spec)
        sys.modules[self.name] = self.module
        try:
            spec.loader.exec_module(self.module)
        except BaseException:
            self.retired = True
            self.unload()
            raise

    def unload(self):
        if self.retired and not self.users:
            for name in list(sys.modules):
                if name == self.name or name.startswith(self.name + "."):
                    del sys.modules[name]

    def release(self):
        self.users -= 1
        self.unload()


class _View(QWidget):
    def __init__(self, controller, key):
        super().__init__(controller.parking)
        self.controller = controller
        self.key = key
        self.setMinimumSize(0, 0)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

    def tab_closing(self):
        instance = self.controller.instances.get(self.key)
        if instance is not None and instance.container is self:
            self.controller.dispose_instance(self.key)


@dataclass
class _Instance:
    extension: UIExtension
    container: _View
    view: QWidget | None = None
    context: dict | None = None
    active: bool = True


class _Popover(OverlayBase):
    CLICK_OUTSIDE_CANCELS = True

    def __init__(self, controller, key):
        self.controller = controller
        self.key = key
        instance = controller.instances[key]
        hint = instance.container.sizeHint()
        super().__init__(controller.host, card_w=max(360, hint.width() + 40),
                         card_h=max(200, hint.height() + 90))
        card, layout = self._make_card("PluginWidgetCard")
        self.scroll = QScrollArea(card)
        self.scroll.setWidgetResizable(True)
        self.scroll.setWidget(instance.container)
        layout.addWidget(self.scroll, 1)
        close = QPushButton(self.tr("Close"), card)
        close.clicked.connect(self._finish)
        layout.addWidget(close, 0, Qt.AlignRight)
        self._present()

    def _finish(self, result=None):
        if self._done:
            return
        widget = self.scroll.takeWidget()
        if widget is not None:
            widget.setParent(self.controller.parking)
        self.controller.popup = None
        super()._finish(result)
        self.controller.queue_layout()


class PluginUIController(QObject):
    def __init__(self, host):
        super().__init__(host)
        from Utils.config_paths import get_config_dir
        self.host = host
        self.area = host._plugin_panel_area
        self.state_path = get_config_dir() / "plugin_ui.json"
        try:
            self.state = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.state = {}
        if not isinstance(self.state, dict):
            self.state = {}
        if not isinstance(self.state.get("visibility"), dict):
            self.state["visibility"] = {}
        self.plugins = {}
        self.extensions = {}
        self.instances = {}
        self.modules = {}
        self.buttons = {}
        self.footers = {}
        self.failed = {}
        self.errors = []
        self.reported = set()
        from gui_qt.runtime.plugin_services import PluginServices
        self.services = PluginServices(self)
        self.popup = None
        self.locked = False
        self.stopped = False
        from gui_qt.runtime.plugin_rows import PluginRows
        self.rows = PluginRows(self)
        from gui_qt.runtime.plugin_menus import PluginMenuController
        self.menus = PluginMenuController(self)
        from gui_qt.runtime.plugin_flags import PluginFlags
        self.flags = PluginFlags(self)
        from gui_qt.runtime.plugin_columns import PluginColumns
        self.columns = PluginColumns(self)
        from gui_qt.runtime.plugin_filters import PluginFilters
        self.filters = PluginFilters(self)
        from gui_qt.runtime.plugin_events import PluginEvents
        self.events = PluginEvents(self)
        from gui_qt.runtime.plugin_selection import PluginSelection
        self.selections = PluginSelection(self)
        from gui_qt.runtime.plugin_notifications import PluginNotifications, PluginDiagnostics
        self.notifications = PluginNotifications(self)
        self.diagnostics = PluginDiagnostics(self)
        from gui_qt.runtime.plugin_previews import PluginPreviews
        self.previews = PluginPreviews(self)
        from gui_qt.runtime.plugin_commands import PluginCommands
        self.commands = PluginCommands(self)
        from gui_qt.runtime.plugin_reads import PluginReads
        self.reads = PluginReads(self)
        from gui_qt.runtime.plugin_operations import PluginOperations
        self.operations = PluginOperations(self)
        self.slot = None
        self.provider_header = None
        self.vertical = False
        self.palette = {}
        self.parking = QWidget(host)
        self.parking.hide()
        self.toolbar = QWidget(self.parking)
        self.toolbar_layout = QBoxLayout(QBoxLayout.LeftToRight, self.toolbar)
        self.toolbar_layout.setContentsMargins(0, 0, 0, 0)
        self.toolbar_layout.setSpacing(4)
        self.overflow = QToolButton(self.toolbar)
        self.overflow.setObjectName("IconButton")
        self.overflow.setText("…")
        self.overflow.setToolTip(self.tr("More plugin controls"))
        self.overflow.setAccessibleName(self.tr("More plugin controls"))
        self.overflow.setPopupMode(QToolButton.InstantPopup)
        self.overflow_menu = QMenu(self.overflow)
        self.overflow.setMenu(self.overflow_menu)
        self.refresh_timer = QTimer(self)
        self.refresh_timer.setSingleShot(True)
        self.refresh_timer.timeout.connect(self.refresh)
        self.layout_timer = QTimer(self)
        self.layout_timer.setSingleShot(True)
        self.layout_timer.timeout.connect(self.rebuild_toolbar)
        self.save_timer = QTimer(self)
        self.save_timer.setSingleShot(True)
        self.save_timer.setInterval(500)
        self.save_timer.timeout.connect(self.save_state)
        for location, tabs in self.panel_tabs():
            tabs.tabCloseRequested.connect(
                lambda i, t=tabs: self.set_panel_visible(t.widget(i).key, False))
        for name, splitter in (("right_sizes", self.area.horizontal),
                               ("bottom_sizes", self.area.vertical)):
            splitter.splitterMoved.connect(
                lambda _pos, _index, n=name, s=splitter: self._save_sizes(n, s))
        bind_theme(self)
        host.installEventFilter(self)
        self.attach_toolbar(host._plugin_toolbar_slot, host._header_vertical())
        self.attach_providers(host._left_header_widget)
        self.attach_footer("modlist_footer", host._modlist_buttons_layout)
        modlist_tab = getattr(host, "_modlist_tab", None)
        if modlist_tab is not None:
            self.attach_footer("modlist_footer", modlist_tab._plugin_buttons_layout)
        for location, layout in host._plugin_footer_layouts.items():
            self.attach_footer(location, layout)
        self.refresh()
        QApplication.instance().aboutToQuit.connect(self.shutdown)

    def panel_tabs(self):
        return (("right_panel", self.area.right), ("bottom_panel", self.area.bottom))

    def valid_target(self):
        game = self.host._gs.game
        return bool(game and game.is_configured())

    def valid_button_target(self, key):
        plugin, extension = self.extensions[key]
        return (self.valid_target() or extension.action == "open_plugin"
                and plugin.ui == "qt" and not plugin.requires_game)

    def _report(self, message):
        if message not in self.reported:
            self.reported.add(message)
            self.host._append_log(f"Plugin UI: {message}")

    def _fail(self, key, exc):
        self.failed[key] = f"{key}: {exc}"
        self._report(self.failed[key])
        self.remove_instance(key)

    def schedule_refresh(self):
        if not self.stopped:
            self.refresh_timer.start(0)

    def refresh_theme(self, palette):
        self.palette = dict(palette)
        self.schedule_refresh()

    def refresh(self):
        if self.stopped or self.locked or getattr(self.host, "_manager_plugin", None) is not None:
            return
        game = self.host._gs.game
        try:
            plugins, self.errors = discover(game.game_id if game else "")
        except OSError as exc:
            plugins, self.errors = [], [str(exc)]
        for error in self.errors:
            self._report(error)
        self.plugins = {p.id: p for p in plugins}
        self.rows.update(plugins)
        self.reads.refresh()
        self.events.refresh(deliver=False)
        self.selections.refresh_target()
        self.flags.update(plugins)
        self.columns.update(plugins)
        self.filters.attach(self.host._modlist_view, "modlist", "modlist")
        self.filters.attach(self.host._plugin_view, "plugins", "plugins")
        tab = getattr(self.host, "_modlist_tab", None)
        if tab is not None:
            self.filters.attach(tab.view, "modlist", "modlist_tab")
        self.filters.update(plugins)
        self.diagnostics.update(plugins)
        self.previews.update(plugins)
        self.commands.update(plugins)
        self.services.refresh(plugins)
        visibility = {p.id: load_data(p) for p in plugins
                      if any(e.game_visibility for e in p.ui_extensions)}
        wanted = {}
        for plugin in plugins:
            for extension in (*plugin.ui_extensions, *self.commands.extensions(plugin)):
                if (extension.location == "toolbar" and extension.action == "run_command"
                        and not self.commands.on_toolbar(plugin, next(c for c in plugin.commands if c.id == extension.target))):
                    continue
                if extension.game_visibility:
                    games = visibility[plugin.id].get(extension.game_visibility, {})
                    if (not isinstance(games, dict) or game is None
                            or games.get(game.game_id) is not True):
                        continue
                wanted[f"{plugin.id}/{extension.id}"] = (plugin, extension)
        valid = self.valid_target()
        for key, instance in list(self.instances.items()):
            entry = wanted.get(key)
            previous = self.extensions.get(key)
            if (not valid or entry is None or instance.extension != entry[1]
                    or previous is None or previous[0] != entry[0]):
                self.remove_instance(key)
        for key in list(self.buttons):
            if key not in wanted or wanted[key][1].location not in ("toolbar", "providers"):
                button = self.buttons.pop(key)
                actions = self.host._action_buttons
                if button in actions:
                    actions.remove(button)
                    self.host._action_btn_widths = False
                button.hide()
                button.deleteLater()
        self.extensions = wanted
        self.failed = {key: error for key, error in self.failed.items() if key in wanted}
        for module_key, module in list(self.modules.items()):
            plugin = self.plugins.get(module_key[0])
            if plugin is None or plugin.entrypoint != module_key[1]:
                self.modules.pop(module_key)
                module.retired = True
                module.unload()
        for key, (plugin, extension) in wanted.items():
            if key in self.failed:
                continue
            if extension.type == "button":
                if extension.location in ("toolbar", "providers"):
                    self.button(key)
            elif valid:
                if (extension.location == "toolbar"
                        or extension.location.endswith("_panel") and self.panel_visible(key)):
                    self.ensure_instance(key)
                instance = self.instances.get(key)
                if instance is not None:
                    context = self.context(key, instance)
                    if context != instance.context:
                        instance.context = context
                        try:
                            instance.view.set_context(context)
                        except Exception as exc:
                            self._fail(key, exc)
        self.rebuild_toolbar()
        self.rebuild_provider_buttons()
        self.rebuild_footer_buttons()
        self.selections.flush()
        self.events.flush()

    def attach_footer(self, location, layout):
        if self.stopped or layout in self.footers:
            return
        self.footers[layout] = (location, {})
        layout.destroyed.connect(lambda: self.footers.pop(layout, None))
        self.rebuild_footer_buttons()

    def detach_footer(self, layout):
        _location, buttons = self.footers.pop(layout, (None, {}))
        for button in buttons.values():
            layout.removeWidget(button)
            button.hide()
            button.deleteLater()

    def rebuild_footer_buttons(self):
        for layout, (location, buttons) in self.footers.items():
            wanted = {key: entry for key, entry in self.extensions.items()
                      if entry[1].location == location}
            for key in list(buttons):
                if key not in wanted:
                    button = buttons.pop(key)
                    layout.removeWidget(button)
                    button.hide()
                    button.deleteLater()
            for key, (plugin, extension) in wanted.items():
                button = buttons.get(key)
                if button is None:
                    button = buttons[key] = self.host._text_button(translate(plugin, extension.label), compact=True)
                    button.setFixedHeight(self.host._FOOT_BTN_H)
                    button.clicked.connect(lambda _checked=False, k=key: self.activate(k))
                else:
                    layout.removeWidget(button)
                button.setText(translate(plugin, extension.label))
                button.setToolTip(translate(plugin, plugin.description))
                button.setAccessibleName(translate(plugin, extension.label))
                image = QIcon(str(extension.icon)) if extension.icon is not None else QIcon()
                button.setIcon(image)
                button.setToolButtonStyle(Qt.ToolButtonTextOnly if image.isNull() else Qt.ToolButtonTextBesideIcon)
                button.setEnabled(not self.locked and self.valid_target() and key not in self.failed)
                layout.addWidget(button)
                button.show()
            layout.invalidate()
            layout.parentWidget().updateGeometry()
        stack = getattr(self.host, "_plugin_footer_stack", None)
        if stack is not None:
            stack.clamp_to_current()

    def context(self, key, instance):
        from amethyst_plugins.i18n import language_code
        plugin, extension = self.extensions[key]
        game = self.host._gs.game
        previous = instance.context or {}
        return {
            "api_version": 1, "plugin_id": plugin.id, "extension_id": extension.id,
            "available_methods": list(plugin.available_methods),
            "host_capabilities": {"reviewed_workflows": True, "maximum_steps": 1024,
                                  "result_references": True, "asynchronous_writes": True},
            "language": language_code(),
            "plugin_dir": str(plugin_directory(plugin)),
            "settings_path": str(settings_path(plugin)), "data_path": str(data_path(plugin)),
            "location": extension.location, "game": game.name, "game_id": game.game_id,
            "profile": self.host._gs.profile, "settings": load_settings(plugin),
            "palette": dict(self.palette),
            "toolbar_orientation": "vertical" if self.vertical else "horizontal",
            "open_plugin": previous.get("open_plugin") or (
                lambda: self.open_plugin(key, instance)),
            "open_tab": previous.get("open_tab") or (
                lambda eid: self.open_tab(f"{plugin.id}/{eid}", origin=instance)),
            "run_command": previous.get("run_command") or (
                lambda cid: self.commands.run(plugin, cid, instance)),
            "read_async": previous.get("read_async") or (
                lambda method, params, callback, **options: self.reads.read(
                    instance, plugin, method, params, callback, **options)),
            "apply_async": previous.get("apply_async") or (
                lambda method, params, callback, **options: self.operations.submit(instance, plugin,
                    [{"method": method, "params": params}], callback, single=True, **options)),
            "apply_batch_async": previous.get("apply_batch_async") or (
                lambda steps, callback, **options: self.operations.submit(instance, plugin, steps, callback, **options)),
            "set_modlist_rows": previous.get("set_modlist_rows") or (
                lambda section, rows, **options: self.rows.publish(instance, plugin, section, rows, **options)),
            "inventory": previous.get("inventory") or (
                lambda: self.inventory(instance)),
            "selection": previous.get("selection") or (
                lambda location=None, **options: self.selection(instance, location, **options)),
            "notify": previous.get("notify") or (
                lambda value: self.notify(instance, plugin, value)),
            "dismiss_notification": previous.get("dismiss_notification") or (
                lambda key: self.notify(instance, plugin, key, dismiss=True)),
            "subscribe": previous.get("subscribe") or (
                lambda names, callback, **options: self.events.subscribe(instance, names, callback, **options)),
        }

    def inventory(self, instance):
        if self.stopped or not instance.active:
            raise RuntimeError("This plugin context has been disposed.")
        return self.events.inventory()

    def selection(self, instance, location=None, **options):
        if self.stopped or not instance.active:
            raise RuntimeError("This plugin context has been disposed.")
        return self.selections.selection(location, **options)

    def notify(self, instance, plugin, value, *, dismiss=False):
        if self.stopped or not instance.active:
            raise RuntimeError("This plugin context has been disposed.")
        game = self.host._gs.game
        target = (game.game_id if game else "", self.host._gs.profile)
        method = self.notifications.dismiss if dismiss else self.notifications.publish
        return method(plugin, *target, value)

    def ensure_instance(self, key):
        if key in self.instances:
            return self.instances[key]
        if key in self.failed or self.locked or not self.valid_target():
            return None
        plugin, extension = self.extensions[key]
        container = _View(self, key)
        instance = _Instance(extension, container)
        self.instances[key] = instance
        try:
            module_key = (plugin.id, plugin.entrypoint)
            module = self.modules.get(module_key)
            if module is None:
                module = self.modules[module_key] = _Module(plugin)
            module.users += 1
            container.destroyed.connect(
                lambda _obj=None, m=module: QTimer.singleShot(0, m.release))
            instance.context = self.context(key, instance)
            view = module.module.create_widget(instance.context, container)
            instance.view = view if isinstance(view, QWidget) else None
            if (not isinstance(view, QWidget) or view.isWindow()
                    or not container.isAncestorOf(view)
                    or not callable(getattr(view, "set_context", None))
                    or not callable(getattr(view, "dispose", None))):
                if isinstance(view, QWidget) and view.parent() is None:
                    view.deleteLater()
                raise ValueError("create_widget must return a child QWidget with set_context() and dispose().")
            container.layout().addWidget(view)
            if extension.location == "plugins_panel":
                self.host._add_plugin_panel_tab(container, translate(plugin, extension.label), key)
            elif extension.location.endswith("_panel"):
                tabs = dict(self.panel_tabs())[extension.location]
                first = tabs.count() == 0
                tabs.addTab(container, translate(plugin, extension.label))
                tabs.show()
                if first:
                    self.restore_sizes(extension.location)
            self.set_instance_enabled(instance)
            return instance
        except Exception as exc:
            self._fail(key, exc)
            return None

    def dispose_instance(self, key):
        instance = self.instances.pop(key, None)
        if instance is None:
            return
        instance.active = False
        self.operations.release(instance)
        self.events.release(instance)
        self.reads.release(instance)
        self.rows.release(instance)
        if instance.view is not None and callable(getattr(instance.view, "dispose", None)):
            try:
                instance.view.dispose()
            except Exception as exc:
                self._report(f"{key}: dispose failed: {exc}")

    def remove_instance(self, key):
        instance = self.instances.get(key)
        if instance is None:
            return
        if self.popup is not None and self.popup.key == key:
            self.popup._finish()
        if instance.extension.location == "tab":
            self.host._tabs.close_tab(self.tab_key(key))
        elif instance.extension.location == "plugins_panel":
            self.host._remove_plugin_panel_tab(instance.container)
        for _location, tabs in self.panel_tabs():
            index = tabs.indexOf(instance.container)
            if index >= 0:
                tabs.removeTab(index)
                tabs.setVisible(tabs.count() > 0)
        self.dispose_instance(key)
        instance.container.hide()
        instance.container.deleteLater()

    def open_plugin(self, key, origin=None):
        if (self.stopped or self.locked or key not in self.extensions
                or not self.valid_button_target(key) or origin is not None and not origin.active):
            return
        if self.popup is not None:
            self.popup._finish()
        self.host._open_manager_plugin(self.extensions[key][0])

    @staticmethod
    def tab_key(key):
        return f"manager-plugin-ui:{key}"

    def open_tab(self, key, origin=None):
        if (self.stopped or self.locked or key not in self.extensions
                or origin is not None and not origin.active):
            return
        plugin, extension = self.extensions[key]
        if extension.location != "tab" or not self.valid_target():
            return
        tab_key = self.tab_key(key)
        if self.host._tabs.has_key(tab_key):
            content = self.host._tabs.content_for_key(tab_key)
            if content is not None:
                self.host._tabs._focus(content)
            return
        instance = self.ensure_instance(key)
        if instance is not None:
            self.host._tabs.open_tab(instance.container, translate(plugin, extension.label), key=tab_key)

    def activate(self, key):
        entry = self.extensions.get(key)
        if entry is None:
            return
        plugin, extension = entry
        if extension.type == "widget":
            self.show_popover(key)
        elif extension.action == "open_tab":
            self.open_tab(f"{plugin.id}/{extension.target}")
        elif extension.action == "run_command":
            self.commands.run(plugin, extension.target)
        else:
            self.open_plugin(key)

    def button(self, key):
        plugin, extension = self.extensions[key]
        button = self.buttons.get(key)
        if button is None:
            button = self.buttons[key] = QToolButton(self.parking)
            button.setObjectName("ActionButton" if extension.location == "providers" else "IconButton")
            button.setCursor(Qt.PointingHandCursor)
            button.clicked.connect(lambda _checked=False, k=key: self.activate(k))
        image_spec = (extension.icon, self.host._ICON_PX)
        if getattr(button, "_plugin_icon_spec", None) != image_spec:
            image = QIcon(str(extension.icon)) if extension.icon is not None else QIcon()
            if image.isNull() or image.pixmap(self.host._ICON_PX).isNull():
                image = icon("wizard.png", self.host._ICON_PX)
            button.setIcon(image)
            button._plugin_icon_spec = image_spec
        button.setIconSize(QSize(self.host._ICON_PX, self.host._ICON_PX))
        if extension.location == "providers":
            button.setFixedHeight(self.host._BTN_H)
        else:
            button.setFixedSize(self.host._BTN_H, self.host._BTN_H)
        button.setToolTip(translate(plugin, extension.label))
        button.setAccessibleName(translate(plugin, extension.label))
        button.setEnabled(not self.locked and self.valid_button_target(key))
        return button

    def attach_providers(self, header):
        self.provider_header = header
        self.rebuild_provider_buttons()

    def rebuild_provider_buttons(self):
        if self.stopped or self.provider_header is None:
            return
        layout = self.provider_header.layout()
        anchor = getattr(self.host, "_wabbajack_btn", None)
        index = layout.indexOf(anchor) + 1 if anchor is not None else -1
        if index <= 0:
            return
        changed = False
        for key, (plugin, extension) in self.extensions.items():
            if extension.location != "providers" or key in self.failed:
                continue
            button = self.buttons[key]
            button.setObjectName("ActionButton")
            button.setText(translate(plugin, extension.label))
            button._full_label = translate(plugin, extension.label)
            button.setMinimumWidth(0)
            button.setMaximumWidth(16777215)
            compact = (self.vertical or self.host._header_force_compact()
                       or bool(button.property("compact")))
            button.setToolButtonStyle(Qt.ToolButtonIconOnly if compact else Qt.ToolButtonTextBesideIcon)
            if button.property("compact") != compact:
                button.setProperty("compact", compact)
                button.style().unpolish(button)
                button.style().polish(button)
            if button not in self.host._action_buttons:
                self.host._action_buttons.append(button)
                changed = True
            if layout.indexOf(button) != index:
                layout.insertWidget(index, button)
                changed = True
            button.show()
            index += 1
        if changed:
            self.host._action_btn_widths = False
        compact = getattr(self.host, "_sync_header_compact", None)
        if compact is not None:
            QTimer.singleShot(0, self.host, compact)

    def attach_toolbar(self, slot, vertical):
        if self.slot is not None:
            self.slot.removeEventFilter(self)
        self.slot = slot
        self.vertical = vertical
        slot.installEventFilter(self)
        self.toolbar.setParent(slot)
        slot.layout().addWidget(self.toolbar)
        self.toolbar_layout.setDirection(QBoxLayout.TopToBottom if vertical else QBoxLayout.LeftToRight)
        self.rebuild_toolbar()
        self.schedule_refresh()

    def park_toolbar(self):
        if self.popup is not None:
            self.popup._finish()
        if self.slot is not None:
            self.slot.removeEventFilter(self)
            self.slot = None
        self.toolbar.setParent(self.parking)
        if self.provider_header is not None:
            layout = self.provider_header.layout()
            for key, button in self.buttons.items():
                if self.extensions[key][1].location == "providers":
                    layout.removeWidget(button)
                    button.hide()
                    button.setParent(self.parking)
            self.provider_header = None

    def eventFilter(self, obj, event):
        if (obj is self.slot or obj is self.host) and event.type() == QEvent.Resize:
            self.queue_layout()
        return super().eventFilter(obj, event)

    def queue_layout(self):
        if not self.stopped:
            self.layout_timer.start(0)

    def rebuild_toolbar(self):
        if self.stopped or self.slot is None:
            return
        self.overflow_menu.clear()
        height = self.host._BTN_H
        budget = height * 4 + 12
        if self.vertical:
            header = self.slot.parentWidget()
            layout = header.layout()
            if layout is not None and layout.indexOf(self.slot) >= 0:
                margins = layout.contentsMargins()
                reserved = margins.top() + margins.bottom()
                count = 0
                for i in range(layout.count()):
                    widget = layout.itemAt(i).widget()
                    hidden = (widget is not None and widget.isHidden()
                              and widget.testAttribute(Qt.WA_WState_ExplicitShowHide))
                    if widget is not None and widget is not self.slot and not hidden:
                        reserved += max(widget.minimumHeight(), widget.sizeHint().height())
                        count += 1
                reserved += (count + 1) * layout.spacing()
                content = getattr(self.host, "_main_content", self.host.centralWidget())
                available = min(self.host.height(), content.height())
                budget = max(height, min(budget, available - reserved))
        items = []
        for key, (plugin, extension) in self.extensions.items():
            if extension.location != "toolbar" or key in self.failed:
                continue
            instance = self.instances.get(key)
            if extension.type == "widget" and instance is None:
                continue
            if instance is not None:
                instance.container.setMinimumHeight(0)
                instance.container.setMaximumHeight(16777215)
            inline = (instance is not None and not self.vertical
                      and instance.container.sizeHint().height() <= height
                      and instance.container.minimumSizeHint().height() <= height
                      and instance.container.sizeHint().width() <= budget - height - 4
                      and (self.popup is None or self.popup.key != key))
            widget = instance.container if inline else self.button(key)
            extent = height if self.vertical else max(height, widget.sizeHint().width())
            items.append((key, widget, extent))
        overflow = sum(item[2] + 4 for item in items) - 4 > budget
        remaining = budget - (height + 4 if overflow else 0)
        visible = []
        for key, widget, extent in items:
            if extent <= remaining:
                widget.setFixedHeight(height)
                if self.vertical:
                    widget.setMinimumWidth(0)
                    widget.setMaximumWidth(16777215)
                visible.append(widget)
                remaining -= extent + 4
            else:
                plugin, extension = self.extensions[key]
                action = self.overflow_menu.addAction(self.button(key).icon(), translate(plugin, extension.label))
                action.setEnabled(not self.locked and self.valid_button_target(key))
                action.triggered.connect(
                    lambda _checked=False, k=key: QTimer.singleShot(0, self, lambda: self.activate(k)))
        self.overflow.setFixedSize(height, height)
        if self.vertical:
            self.overflow.setMinimumWidth(0)
            self.overflow.setMaximumWidth(16777215)
        self.overflow.setEnabled(not self.locked)
        if self.overflow_menu.actions():
            visible.append(self.overflow)
        for index in reversed(range(self.toolbar_layout.count())):
            widget = self.toolbar_layout.itemAt(index).widget()
            if widget not in visible:
                self.toolbar_layout.takeAt(index)
                widget.hide()
                if widget is not self.overflow:
                    widget.setParent(self.parking)
        for index, widget in enumerate(visible):
            if self.toolbar_layout.indexOf(widget) != index:
                self.toolbar_layout.insertWidget(index, widget)
            widget.show()
        if self.vertical:
            self.toolbar.setMaximumSize(16777215, budget)
            self.toolbar.setMinimumSize(0, height if items else 0)
            self.slot.setMinimumHeight(height if items else 0)
            self.toolbar.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        else:
            self.toolbar.setMaximumSize(budget, height)
            self.toolbar.setMinimumSize(0, height if items else 0)
            self.slot.setMinimumHeight(0)
            self.toolbar.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Fixed)
        self.toolbar.setVisible(bool(items))
        self.slot.setVisible(bool(items))
        compact = getattr(self.host, "_sync_header_compact", None)
        if compact is not None:
            QTimer.singleShot(0, self.host, compact)

    def show_popover(self, key):
        if self.locked or self.stopped or key not in self.instances:
            return
        if self.popup is not None:
            self.popup._finish()
        self.popup = _Popover(self, key)

    def panel_visible(self, key):
        return self.state["visibility"].get(key, True) is not False

    def set_panel_visible(self, key, visible):
        if self.locked or self.stopped:
            return
        self.state["visibility"][key] = bool(visible)
        if not visible:
            self.remove_instance(key)
        self.save_timer.start()
        self.refresh()

    def restore_sizes(self, location):
        right = location == "right_panel"
        splitter = self.area.horizontal if right else self.area.vertical
        name = "right_sizes" if right else "bottom_sizes"
        sizes = self.state.get(name)
        if (not isinstance(sizes, list) or len(sizes) != 2
                or any(type(n) is not int or n < 0 for n in sizes) or not sum(sizes)):
            total = splitter.width() if right else splitter.height()
            panel = min(280 if right else 180, max(1, total // 3))
            sizes = [max(1, total - panel), panel]
        splitter.setSizes(sizes)

    def _save_sizes(self, name, splitter):
        self.state[name] = splitter.sizes()
        self.save_timer.start()

    def save_state(self):
        from Utils.atomic_write import write_atomic
        try:
            write_atomic(self.state_path, json.dumps(self.state, ensure_ascii=True).encode())
        except OSError as exc:
            self._report(f"Could not save interface state: {exc}")

    def add_menu(self, menu):
        if self.commands.entries:
            action = menu.addAction(self.tr("Commands…"))
            action.setEnabled(not self.locked and self.valid_target())
            action.triggered.connect(lambda _checked=False: self.commands.show())
        if not self.extensions and not self.failed and not self.services.failed:
            return
        for key, (plugin, extension) in self.extensions.items():
            if not plugin.show_in_menu:
                continue
            if extension.type == "button" or extension.location == "toolbar":
                continue
            if extension.id == plugin.menu_tab:
                continue
            action = menu.addAction(f"{translate(plugin, plugin.name)}: {translate(plugin, extension.label)}")
            action.setEnabled(not self.locked and self.valid_target() and key not in self.failed)
            if extension.location == "tab":
                action.triggered.connect(lambda _checked=False, k=key: self.open_tab(k))
            else:
                action.setCheckable(True)
                action.setChecked(self.panel_visible(key))
                action.triggered.connect(lambda checked, k=key: self.set_panel_visible(k, checked))
        if self.failed:
            action = menu.addAction(self.tr("Some interface elements could not be loaded…"))
            action.triggered.connect(lambda _checked=False: self.host._show_warn_popup(
                self.tr("Plugin interface"), "\n".join(self.failed.values()), None))
        if self.services.failed:
            action = menu.addAction(self.tr("Some plugin services could not be loaded…"))
            action.triggered.connect(lambda _checked=False: self.host._show_warn_popup(
                self.tr("Plugin services"), "\n".join(self.services.failed.values()), None))

    def set_instance_enabled(self, instance):
        instance.container.setEnabled(not self.locked and self.valid_target())
        window = instance.container.window()
        if window is not self.host and window is not self.parking:
            window.setEnabled(not self.locked)

    def set_locked(self, locked):
        if locked:
            self.commands.close()
        self.menus.set_locked(locked)
        self.previews.set_locked(locked)
        self.locked = locked
        if locked and self.popup is not None:
            self.popup._finish()
        self.toolbar.setEnabled(not locked)
        for key, button in self.buttons.items():
            if self.extensions[key][1].location == "providers":
                button.setEnabled(not locked and self.valid_target())
        for _location, buttons in self.footers.values():
            for key, button in buttons.items():
                button.setEnabled(not locked and self.valid_target() and key not in self.failed)
        for instance in self.instances.values():
            self.set_instance_enabled(instance)
        if not locked:
            self.schedule_refresh()

    def shutdown(self):
        if self.stopped:
            return
        self.stopped = True
        self.menus.shutdown()
        self.flags.shutdown()
        self.columns.shutdown()
        self.filters.shutdown()
        self.events.shutdown()
        self.selections.shutdown()
        self.diagnostics.shutdown()
        self.notifications.shutdown()
        self.previews.shutdown()
        self.commands.shutdown()
        self.reads.shutdown()
        self.operations.shutdown()
        self.services.shutdown()
        self.refresh_timer.stop()
        self.layout_timer.stop()
        self.save_timer.stop()
        self.save_state()
        for key, button in self.buttons.items():
            if self.extensions[key][1].location == "providers":
                if button in self.host._action_buttons:
                    self.host._action_buttons.remove(button)
                button.hide()
                button.deleteLater()
        for layout in list(self.footers):
            self.detach_footer(layout)
        for key in list(self.instances):
            self.remove_instance(key)
        for module in self.modules.values():
            module.retired = True
            module.unload()
        self.modules.clear()
