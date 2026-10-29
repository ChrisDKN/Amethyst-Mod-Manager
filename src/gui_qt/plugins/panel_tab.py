from copy import deepcopy

from PySide6.QtCore import QEvent
from PySide6.QtWidgets import QWidget, QHBoxLayout, QVBoxLayout, QLineEdit

from gui_qt.plugins.panel_controls import PluginPanelControls
from gui_qt.widgets.filter_panel import FilterSidePanel


PANEL_ATTRS = ("_plugin_view", "_mod_files_view", "_text_files_view",
               "_data_view", "_downloads_view", "_overrides_view", "_saves_view")
PANEL_KEYS = ("plugins", "mod_files", "text_files", "data", "downloads",
              "overrides", "saves")


class PluginPanelTab(QWidget, PluginPanelControls):
    def __init__(self, host, index):
        super().__init__()
        self.host = host
        self.index = index
        self.panel_attr = PANEL_ATTRS[index]
        self._closing = False
        self._connections = []
        self._footer_layouts = []
        for name in ("_FOOT_BTN_H", "_text_button", "_color_button", "_action_button",
                     "_equalize_button_widths", "_filters_button", "_search_icon_label",
                     "_notify", "_open_folder_path", "_on_sort_plugins",
                     "_on_refresh_plugins", "_on_sync_plugins", "_open_loot_report",
                     "_open_plugin_groups_tab", "_open_data_blacklist", "_open_data_routing_rules"):
            setattr(self, name, getattr(host, name))
        if index == 0:
            from gui_qt.plugins.presentation import PluginPresentation
            from gui_qt.plugins.view import PluginView
            self._plugin_model = PluginPresentation(host._plugin_model, self)
            self.view = PluginView(self._plugin_model, persist_view=False)
            self.view.header().restoreState(host._plugin_view.header().saveState())
            self._connect(self._plugin_model.refreshed, self.refresh_plugins)
            self.view.selectionModel().selectionChanged.connect(
                lambda *_: host._on_plugin_selection_changed(self.view))
        else:
            self.view = host._create_plugin_panel_view(
                index, selection_source_id=f"{PANEL_KEYS[index]}_tab")
        setattr(self, PANEL_ATTRS[index], self.view)

        self.layout_row = QHBoxLayout(self)
        self.layout_row.setContentsMargins(0, 0, 0, 0)
        column = QVBoxLayout()
        column.setContentsMargins(0, 0, 0, 0)
        column.addWidget(self.view, 1)
        footer = getattr(self, f"_{PANEL_KEYS[index]}_footer")()
        host._enable_height_for_width(footer)
        column.addWidget(footer)
        self.layout_row.addLayout(column, 1)
        searches = footer.findChildren(QLineEdit)
        self.search = searches[-1] if searches else None
        if self.search is not None:
            self.search.installEventFilter(self)
        self._build_filters()
        if index == 4:
            self.view.on_move = self._on_downloads_move
            self.view.on_toggle_hidden = self._on_downloads_toggle_hidden
            self.view.on_remove = self._on_downloads_remove
            self.view.selection_changed.connect(self._update_downloads_footer)
            self._update_downloads_footer()
        if index == 1:
            self.view.mod_changed.connect(lambda *_: self._update_mf_footer_buttons())
            self._connect(host._modlist_model.dataChanged, self._mod_files_updated)
        self.configure()
        if index == 1 and self._bsa_op_running:
            host._bsa_widgets.append((self.view, self.view.isEnabled()))
            self.view.setEnabled(False)
        if index == 0:
            self.refresh_plugins()
        self.view.installEventFilter(self)
        self.view.on_open_as_tab = lambda: host._open_plugin_panel_tab(index)
        tree = self.view if index == 0 else self.view._tree
        tree.installEventFilter(self)
        tree.viewport().installEventFilter(self)
        from gui_qt.runtime.plugin_selection import bind_selection
        if index != 5:
            bind_selection(self.view, tree, PANEL_KEYS[index], f"{PANEL_KEYS[index]}_tab")
        from gui_qt.runtime.shortcuts import register_panel_tab_shortcuts
        register_panel_tab_shortcuts(host, self.view, panel="plugin" if index == 0 else "files")

    @property
    def _pal(self):
        return self.host._pal

    @property
    def _bsa_op_running(self):
        return self.host._bsa_op_running

    def tr(self, text, disambiguation=None, n=-1):
        return self.host.tr(text, disambiguation, n)

    def _connect(self, signal, callback):
        signal.connect(callback)
        self._connections.append((signal, callback))

    def _mod_files_updated(self, *_args):
        self._update_mf_footer_buttons()

    def _register_plugin_footer(self, location, layout):
        self._footer_layouts.append(layout)
        controller = getattr(self.host, "_plugin_ui", None)
        if controller is not None:
            controller.attach_footer(location, layout)

    def configure(self):
        host, view, index = self.host, self.view, self.index
        game, profile = host._gs.game, host._gs.profile_dir()
        snapshot = getattr(getattr(host, "_conflict_data", None), "snapshot", None)
        if index in (1, 2):
            view.configure(game, profile)
            view.set_snapshot(snapshot)
            if index == 1:
                view.show_mod(getattr(host._mod_files_view, "_mod_name", None))
                self._update_mf_footer_buttons()
        elif index == 3:
            view.configure(game, profile, snapshot=snapshot)
        elif index == 4:
            view.configure(game, lambda: host._gs.game_name, profile)
            view.set_active_downloads(host._active_download_rows())
        elif index == 5:
            view.configure(profile, host._gs.staging_dir(), host._gs.modlist_path())
        elif index == 6:
            view.configure(game, host._gs.profile or "")
            view.set_known_plugins([row.name for row in host._plugin_model.natural_rows()])

    def _build_filters(self):
        if self.index == 5:
            self.filters = None
            return
        if self.index == 0:
            from gui_qt.modlist.filter import PLUGIN_STATUS_FILTERS
            spec = [{"title": "By status", "type": "checks",
                     "items": [(key, label, True) for key, label in PLUGIN_STATUS_FILTERS]}]
        else:
            spec = self.view.filter_spec()
        self.filters = FilterSidePanel(spec, title=self.host.tr("Filters"))
        self.view.panel_filters = self.filters
        self.filters.hide()
        self.filters.changed.connect(self._filters_changed)
        self.filters.close_requested.connect(self._toggle_filters)
        self.layout_row.insertWidget(0, self.filters)
        self.filters.set_state(deepcopy(self.host._filter_states.get(PANEL_KEYS[self.index], {})), emit=False)
        if self.index == 0:
            self.view.on_quick_filter = self.filters.set_check
            self.view.quick_filter_state = self.filters.check_state
            self.view.filters_active = self.filters.any_active
            self.view.on_clear_filters = self.filters.clear_all
            controller = getattr(self.host, "_plugin_ui", None)
            if controller is not None:
                controller.filters.attach(self.view, "plugins", "plugins_tab")
                controller.filters.attach_panel(self.view, self.filters)
        else:
            from gui_qt.widgets.filter_menu_button import FilterMenuButton, BTN_W
            self.view._tree.header_left_pad = BTN_W
            button = FilterMenuButton(self.view._tree)
            dynamic = {key: getattr(self.view, method) for key, method in (
                ("filetypes", "filetype_items"), ("locations", "location_items"))
                if hasattr(self.view, method)}
            button.sections_fn = lambda: self.host._filter_menu_sections(
                self.filters, spec, dynamic, self._sync_dynamic_filters)
            button.on_toggle = lambda key, on: self.host._on_filter_menu_toggle(self.filters, key, on)
            button.on_clear = self.filters.clear_all
            button.any_active = self.filters.any_active
            button.columns_fn = getattr(self.view, "column_menu_items", None)
            button.on_column_toggle = getattr(self.view, "set_column_visible", None)
            button.on_open_as_tab = lambda: self.host._open_plugin_panel_tab(self.index)
            self.view.filetypes_changed.connect(self._sync_dynamic_filters)
        self._filters_changed(self.filters.state())

    def _sync_dynamic_filters(self, *, force=False):
        for key, method in (("filetypes", "filetype_items"), ("locations", "location_items")):
            if hasattr(self.view, method):
                self.filters.set_dynamic_items(key, getattr(self.view, method)())

    def _filters_changed(self, state):
        if self.index == 0:
            self._apply_plugin_search()
        else:
            self.view.apply_filter_state(state)
        self._sync_filters_btn()

    def _toggle_filters(self):
        self._sync_dynamic_filters()
        self.filters.setVisible(not self.filters.isVisible())

    _toggle_plugin_filters = _toggle_filters
    _toggle_mod_files_filters = _toggle_filters
    _toggle_text_files_filters = _toggle_filters
    _toggle_data_filters = _toggle_filters
    _toggle_downloads_filters = _toggle_filters
    _toggle_saves_filters = _toggle_filters

    def _sync_filters_btn(self, _attr=None):
        if not hasattr(self, "filters") or self.filters is None:
            return
        active = self.filters.any_active() or bool(self.search and self.search.text().strip())
        if self.index == 0:
            from gui_qt.runtime.plugin_filters import plugin_filters_active
            active = active or plugin_filters_active(self.view)
        for name in ("_plugin_filters_btn", "_mf_filters_btn", "_tf_filters_btn",
                     "_data_filters_btn", "_dl_filters_btn", "_saves_filters_btn"):
            button = getattr(self, name, None)
            if button is not None:
                button.setProperty("active", active)
                button.style().unpolish(button)
                button.style().polish(button)

    def _on_plugin_search(self, _text):
        if hasattr(self, "filters"):
            self._apply_plugin_search()

    def _apply_plugin_search(self):
        from gui_qt.modlist.filter import plugin_filter_hidden_rows, plugin_search_hidden_rows
        rows = self._plugin_model._rows
        data = getattr(self.host, "_conflict_data", None)
        self.view.set_search_hidden(plugin_search_hidden_rows(
            rows, self.search.text(), data.plugin_owner if data else {}))
        self.view.set_filter_hidden(plugin_filter_hidden_rows(
            rows, self.filters.state(), disabled_mf=self.host._disabled_plugin_files()))

    def refresh_plugins(self):
        if self._closing or not hasattr(self, "filters"):
            return
        source = self.host._plugin_view
        for name in ("game", "profile_dir", "resolved_paths", "userlist_plugins",
                     "userlist_cycles", "on_plugins_changed", "on_dirty_flag_clicked",
                     "on_group_add", "on_userlist_remove", "on_show_cycle", "on_show_overlapping"):
            if hasattr(source, name):
                setattr(self.view, name, getattr(source, name))
        self.view.on_userlist_add = lambda name, _row: self.host._on_userlist_add(
            name, next((i for i, row in enumerate(self.host._plugin_model._rows)
                        if row.name == name), -1))
        self.view.set_plugin_owner(source._plugin_owner)
        self.view.refresh_missing_marker()
        self.view.refresh_cycle_marker()
        self._refresh_plugin_stats()
        self._apply_plugin_search()
        for attr in ("_plugin_sort_btn", "_plugin_refresh_btn", "_plugin_sync_btn"):
            original = getattr(self.host, attr, None)
            if original is not None:
                getattr(self, attr).setEnabled(original.isEnabled())
                getattr(self, attr).setText(original.text())
        self._plugin_loot_btn.setVisible(bool(getattr(self.host._gs.game, "loot_sort_enabled", False)))

    def _open_plugin_rules_tab(self):
        self.host._open_plugin_rules_tab(self.view)

    def _on_pack_bsa(self):
        self.host._on_pack_bsa(self.view)

    def _on_unpack_bsa(self):
        self.host._on_unpack_bsa(self.view)

    def _on_mf_reset_clicked(self):
        self.host._on_mf_reset_clicked(self.view)

    def _on_downloads_locations(self):
        self.host._on_downloads_locations()

    def _on_downloads_move(self, paths=None):
        self.host._on_downloads_move(paths, view=self.view)

    def _on_downloads_remove(self, paths=None):
        self.host._on_downloads_remove(paths, view=self.view)

    def _on_downloads_toggle_hidden(self, paths=None, hidden=None):
        self.host._on_downloads_toggle_hidden(paths, hidden, view=self.view)

    def showEvent(self, event):
        super().showEvent(event)
        if hasattr(self.view, "set_visible_tab"):
            self.view.set_visible_tab(True)
        if self.index == 0:
            self.refresh_plugins()
        elif self.index == 6:
            self._sync_saves_buttons()

    def hideEvent(self, event):
        if hasattr(self.view, "set_visible_tab"):
            self.view.set_visible_tab(False)
        super().hideEvent(event)

    def eventFilter(self, watched, event):
        if event.type() in (QEvent.FocusIn, QEvent.MouseButtonPress):
            self.host._last_plugin_panel_tab = self
            if self.index == 0:
                self.host._last_list_panel = "plugin"
                self.host._last_plugin_view = self.view
        return False

    def tab_closing(self):
        if self._closing:
            return
        self._closing = True
        self.host._plugin_panel_tabs.pop(self.index, None)
        self.host._bsa_widgets = [(view, enabled) for view, enabled in getattr(self.host, "_bsa_widgets", [])
                                  if view is not self.view]
        if getattr(self.host, "_last_plugin_panel_tab", None) is self:
            self.host._last_plugin_panel_tab = None
        if getattr(self.host, "_last_plugin_view", None) is self.view:
            self.host._last_plugin_view = self.host._plugin_view
        controller = getattr(self.host, "_plugin_ui", None)
        if controller is not None:
            for layout in self._footer_layouts:
                controller.detach_footer(layout)
        for signal, callback in self._connections:
            signal.disconnect(callback)
        from gui_qt.runtime.shortcuts import unregister_panel_tab_shortcuts
        unregister_panel_tab_shortcuts(self.host, self.view)
        if self.index == 0:
            self._plugin_model.dispose()
        elif self.index == 6 and self.view._busy:
            self.view.busy_changed.disconnect(self._sync_saves_buttons)
            self.view.selection_changed.disconnect(self._sync_saves_buttons)
            self.view.status_changed.disconnect(self._saves_status.setText)
            self.view.notify.disconnect(self._on_saves_notify)
            self.view.notify.connect(self.host._notify)
            self.view.set_visible_tab(False)
            self.view.setParent(self.host)
            self.view.hide()
            self.view.busy_changed.connect(lambda ready, view=self.view: view.deleteLater() if ready else None)
