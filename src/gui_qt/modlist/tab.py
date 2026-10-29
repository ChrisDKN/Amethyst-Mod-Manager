"""The optional second modlist panel."""

from copy import deepcopy

from PySide6.QtCore import QEvent
from PySide6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit

from gui_qt.widgets.filter_panel import FilterSidePanel
from gui_qt.widgets.flow_layout import FlowLayout
from gui_qt.modlist.filter import (
    STATUS_FILTERS, FilterData, compute_hidden_rows, conflict_filter_hidden_rows,
    search_hidden_rows,
)
from gui_qt.modlist.presentation import ModListPresentation
from gui_qt.modlist.view import ModListView


class ModListTab(QWidget):
    def __init__(self, window):
        super().__init__()
        self.window = window
        self._refreshing = False
        self._context = getattr(window, "_modlist_context", None)
        self.state = deepcopy(getattr(window, "_modlist_filter_state", {}))
        self.conflict_names = set(getattr(window, "_modlist_conflict_filter", ()) or ())
        self.conflict_anchor = getattr(window, "_modlist_conflict_filter_anchor", "")
        self.model = ModListPresentation(window._modlist_model, self)
        self.view = ModListView(self.model, persist_view=False)
        self.view.header().restoreState(window._modlist_view.header().saveState())
        self.model.set_sort(*window._modlist_model.sort_state())

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.filters = FilterSidePanel([
            {"title": "By status", "type": "checks",
             "items": [(key, label, True) for key, label in STATUS_FILTERS]},
            {"title": "By source location", "type": "dynamic", "id": "sources"},
            {"title": "By category", "type": "dynamic", "id": "categories"},
            {"title": "By file type", "type": "dynamic", "id": "filetypes"},
            {"title": "By author", "type": "dynamic", "id": "authors"},
        ], title=window.tr("Filters"))
        self.filters.set_state(self.state, emit=False)
        self.filters.changed.connect(self._filters_changed)
        self.filters.cleared.connect(self.clear_conflicts)
        self.filters.close_requested.connect(self.toggle_filters)
        self.filters.hide()
        layout.addWidget(self.filters)
        column = QVBoxLayout()
        column.setContentsMargins(0, 0, 0, 0)
        column.addWidget(self.view, 1)
        column.addWidget(self._footer())
        layout.addLayout(column, 1)
        self.view.selectionModel().selectionChanged.connect(
            lambda *_: window._on_mod_selection_changed(self.view))
        self.view.installEventFilter(self)
        self.view.viewport().installEventFilter(self)
        self._position = None
        self._filter_data = None
        self._connections = []
        for model in (window._modlist_model, self.model):
            for signal in (model.layoutAboutToBeChanged, model.modelAboutToBeReset,
                           model.rowsAboutToBeMoved, model.rowsAboutToBeInserted,
                           model.rowsAboutToBeRemoved):
                signal.connect(self._remember_positions)
                self._connections.append((signal, self._remember_positions))
            for signal in (model.layoutChanged, model.modelReset, model.rowsMoved,
                           model.rowsInserted, model.rowsRemoved):
                signal.connect(self._restore_positions)
                self._connections.append((signal, self._restore_positions))
        self.model.refreshed.connect(self.refresh)
        self.model.groups_changed.connect(self.refresh)
        self.search.setText(getattr(window, "_modlist_search_text", ""))
        self.refresh_conflicts()
        self.refresh()
        controller = getattr(window, "_plugin_ui", None)
        if controller is not None:
            controller.attach_footer("modlist_footer", self._plugin_buttons_layout)
            controller.filters.attach(self.view, "modlist", "modlist_tab")
            controller.filters.attach_panel(self.view, self.filters)

    def reset_context(self):
        main = self.window._modlist_model
        self.model._collapsed = set(main._collapsed)
        self.model._mod_groups = deepcopy(main._mod_groups)
        self.model._index_groups()
        self.model.set_highlights()
        self.view.clearSelection()
        self.view.scrollToTop()
        self.conflict_names.clear()
        self.conflict_anchor = ""
        self._position = None

    def refresh_conflicts(self, changed_mods=None):
        main = self.window._modlist_view
        maps = (main._overrides, main._overridden_by,
                main._bsa_overrides, main._bsa_overridden_by)
        if changed_mods is None:
            self.view.set_conflict_maps(*maps)
        else:
            self.view.apply_conflict_map_delta(*maps, changed_mods)

    def eventFilter(self, watched, event):
        if event.type() in (QEvent.FocusIn, QEvent.MouseButtonPress):
            self.window._last_list_panel = "mod"
            self.window._last_plugin_panel_tab = None
            self.window._last_modlist_view = self.view
        return False

    def _footer(self):
        win = self.window
        bar = QWidget()
        bar.setObjectName("HeaderBar")
        layout = QVBoxLayout(bar)
        layout.setContentsMargins(8, 6, 8, 6)
        buttons = FlowLayout(spacing=4, center=True)
        self._plugin_buttons_layout = buttons
        self.expand = win._text_button(win.tr("Expand all"), compact=True)
        self.enable = win._text_button(win.tr("Enable all"), compact=True)
        self.expand.clicked.connect(self.toggle_collapse)
        self.enable.clicked.connect(self.toggle_enabled)
        buttons.addWidget(self.expand)
        buttons.addWidget(self.enable)
        for label, callback in ((win.tr("Check Updates"), win._on_check_updates),
                                (win.tr("Restore backup"), win._open_restore_backup_tab),
                                (win.tr("Refresh Modlist"), win._on_refresh_modlist)):
            button = win._text_button(label, compact=True)
            button.clicked.connect(lambda _checked=False, cb=callback: cb())
            buttons.addWidget(button)
        layout.addLayout(buttons)
        row = QHBoxLayout()
        self.count = QLabel()
        self.count.setObjectName("ModlistCountLabel")
        self.count.setStyleSheet(win._modlist_count.styleSheet())
        row.addWidget(self.count)
        self.filter_button = win._filters_button()
        self.filter_button.clicked.connect(self.toggle_filters)
        row.addWidget(self.filter_button)
        row.addWidget(win._search_icon_label(win._modlist_search_tooltip()))
        self.search = QLineEdit()
        self.search.setPlaceholderText(win._modlist_search.placeholderText())
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self._search_changed)
        self.search.installEventFilter(self)
        row.addWidget(self.search, 1)
        layout.addLayout(row)
        win._enable_height_for_width(bar)
        return bar

    def _remember_positions(self, *_args):
        if self._position is None:
            self._position = [(v, v.capture_position())
                              for v in (self.window._modlist_view, self.view)]

    def _restore_positions(self, *_args):
        if self._position is None:
            return
        positions, self._position = self._position, None
        self.apply_filters()
        for view, position in positions:
            view.restore_position(position)

    def toggle_filters(self):
        self.filters.setVisible(not self.filters.isVisible())
        self.refresh()

    def toggle_collapse(self):
        self.view.set_all_collapsed(not self.model.any_collapsed())
        self.refresh()

    def toggle_enabled(self):
        self.model.set_all_enabled(not self.model.all_mods_enabled())
        self.refresh()

    def _search_changed(self, _text):
        self.apply_filters()
        if "!" in self.search.text() and self.window._modlist_filter_data is None:
            self.window._rebuild_filter_data()

    def _filters_changed(self, state):
        self.state = state
        self.apply_filters()

    def clear_conflicts(self):
        self.conflict_names.clear()
        self.conflict_anchor = ""
        self.apply_filters()

    def clear_filters(self):
        self.clear_conflicts()
        self.filters.clear_all()

    def filter_conflicts(self, name):
        if name == self.conflict_anchor:
            self.clear_conflicts()
            return
        higher, lower = self.view.conflict_partners([name])
        others = (higher | lower) - {name}
        if not others:
            self.window._notify(self.window.tr("{0} has no conflicting mods.").format(name), "info")
            return
        self.conflict_anchor = name
        self.conflict_names = others | {name}
        self.apply_filters()

    def apply_filters(self):
        if not hasattr(self, "search"):
            return
        data = getattr(self.window, "_modlist_filter_data", None) or FilterData()
        data.nexus_mod_ids = dict(self.model._nexus_mod_ids)
        data.nexus_file_ids = dict(self.model._nexus_file_ids)
        self.model.set_separators_hidden(self.state.get("filter_hide_separators") == 1)
        entries = self.model._entries
        self.view.set_search_hidden(search_hidden_rows(entries, self.search.text(), data),
                                    active=bool(self.search.text().strip()))
        self.view.set_filter_hidden(compute_hidden_rows(entries, self.state, data)
                                    | conflict_filter_hidden_rows(entries, self.conflict_names))
        from gui_qt.runtime.plugin_filters import plugin_filters_active
        active = bool(self.search.text().strip() or self.filters.any_active() or self.conflict_names
                      or plugin_filters_active(self.view))
        if bool(self.filter_button.property("active")) != active:
            self.filter_button.setProperty("active", active)
            self.filter_button.style().unpolish(self.filter_button)
            self.filter_button.style().polish(self.filter_button)

    def refresh(self):
        if self._refreshing:
            return
        self._refreshing = True
        try:
            win, view, model = self.window, self.view, self.model
            main = win._modlist_view
            context = getattr(win, "_modlist_context", None)
            if context != self._context:
                self._context = context
                self.conflict_names.clear()
                self.conflict_anchor = ""
                model._collapsed = set(main.model()._collapsed)
                model._mod_groups = deepcopy(main.model()._mod_groups)
                model.set_highlights()
                view.clearSelection()
                self._position = None
            model.modlist_path = win._modlist_model.modlist_path
            for attr, value in vars(main).items():
                if attr.startswith("on_"):
                    setattr(view, attr, value)
            for attr in ("staging_dir", "profile_dir", "game", "suppress_conflict_highlights"):
                if hasattr(main, attr):
                    setattr(view, attr, getattr(main, attr))
            view.on_flag_clicked = lambda row, flag: win._on_modlist_flag_clicked(row, flag, view)
            view.on_filter_conflicts = self.filter_conflicts
            view.on_clear_conflict_filter = self.clear_conflicts
            view.conflict_filter_anchor = lambda: self.conflict_anchor
            view.on_clear_filters = self.clear_filters
            view.on_quick_filter = self.filters.set_check
            view.quick_filter_state = self.filters.check_state
            view.quick_filter_enabled = main.quick_filter_enabled
            view.filters_active = lambda: self.filters.any_active() or bool(self.conflict_names)
            view.on_archives_dropped = lambda paths, slot, separator=None: win._install_paths(
                paths, clear_archives=False,
                place=win._placement_from_slot(slot, separator, model=model))
            data = getattr(win, "_modlist_filter_data", None)
            if data is not None and data is not self._filter_data:
                self._filter_data = data
                categories = sorted({c or "" for c in data.category_names.values()} | {""},
                                    key=str.casefold)
                self.filters.set_dynamic_items("categories", [
                    (c, c or win.tr("(Uncategorized)"), None) for c in categories])
                self.filters.set_dynamic_items("authors", [
                    (a, a, None) for a in sorted({a for a in data.author_names.values() if a},
                                                key=str.casefold)])
                self.filters.set_dynamic_items("filetypes", [
                    (ext, ext, count) for ext, count in sorted(data.filetype_counts.items())])
                self.filters.set_dynamic_items("sources", [
                    (key, label, None) for key, label in (
                        ("nexus", win.tr("Nexus")), ("thunderstore", win.tr("Thunderstore")),
                        ("modio", win.tr("mod.io")), ("none", win.tr("None")))])
            for key, _label in STATUS_FILTERS:
                self.filters.set_check_enabled(key, main.quick_filter_enabled(key)
                                                if main.quick_filter_enabled else True)
            extensions = getattr(view.game, "archive_extensions", ()) or ()
            archive = "BA2" if ".ba2" in extensions else "PAK" if ".pak" in extensions else "BSA"
            self.filters.set_check_label("filter_has_bsa", win.tr("Mods with {0} archives").format(archive))
            self.filters.set_check_enabled("filter_has_bsa", bool(extensions))
            self.filters.set_check_enabled("filter_has_pbr", getattr(
                view.game, "nexus_game_domain", "") == "skyrimspecialedition")
            self.state = self.filters.state()
            self.apply_filters()
            self.expand.setEnabled(main.isEnabled() and not model.flat_sort_active)
            self.enable.setEnabled(main.isEnabled())
            self.expand.setText(win.tr("Expand all") if model.any_collapsed() else win.tr("Collapse all"))
            self.enable.setText(win.tr("Disable all") if model.all_mods_enabled() else win.tr("Enable all"))
            mods = [e for e in model._natural if not e.is_separator and not e.is_group_header]
            self.count.setText(f"{sum(e.enabled for e in mods)} / {len(mods)}")
            view.setEnabled(main.isEnabled())
        finally:
            self._refreshing = False

    def tab_closing(self):
        if getattr(self, "_closing", False):
            return
        self._closing = True
        from gui_qt.runtime.shortcuts import unregister_panel_tab_shortcuts
        self.drag.dispose()
        self.dispose()
        unregister_panel_tab_shortcuts(self.window, self.view)
        if getattr(self.window, "_last_modlist_view", None) is self.view:
            self.window._last_modlist_view = self.window._modlist_view
        if getattr(self.window, "_modlist_tab", None) is self:
            self.window._modlist_tab = None

    def dispose(self):
        controller = getattr(self.window, "_plugin_ui", None)
        if controller is not None:
            controller.detach_footer(self._plugin_buttons_layout)
        for signal, callback in self._connections:
            signal.disconnect(callback)
        self._connections.clear()
        self.model.dispose()
