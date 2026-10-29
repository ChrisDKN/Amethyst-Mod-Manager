from __future__ import annotations

import weakref
from dataclasses import replace

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QMenu, QPushButton

from amethyst_plugins.i18n import translate
from amethyst_plugins.metadata import provider_context
from amethyst_plugins.catalog import load_data, load_settings, plugin_directory
from gui_qt.runtime.plugin_flags import PluginFlags


def evaluate_filters(plugin, snapshot):
    from gui_qt.runtime.plugin_ui import _Module
    module = _Module(replace(plugin, entrypoint=plugin.filter_provider))
    try:
        context = dict(provider_context(plugin, snapshot), plugin_id=plugin.id,
                       plugin_dir=str(plugin_directory(plugin)),
                       settings=load_settings(plugin), data=load_data(plugin))
        result = module.module.evaluate_filters(context)
        if not isinstance(result, dict) or set(result) - {"modlist", "plugins"}:
            raise ValueError("evaluate_filters must return modlist/plugins row mappings.")
        output = {}
        for panel in ("modlist", "plugins"):
            declared = {f.id for f in plugin.filters if panel in f.panels}
            rows = result.get(panel, {})
            known = {row["name"] for row in snapshot[panel]}
            if not isinstance(rows, dict):
                raise ValueError("Filter results must map row names to filter ID lists.")
            output[panel] = {None: tuple(f"{plugin.id}/{fid}" for fid in declared)}
            for name, ids in rows.items():
                if (name not in known or not isinstance(ids, list) or len(ids) > 32
                        or any(not isinstance(fid, str) or fid not in declared for fid in ids)):
                    raise ValueError(f"Invalid row or filter ID: {name}")
                output[panel][name] = tuple(f"{plugin.id}/{fid}" for fid in ids)
        return output
    finally:
        module.retired = True
        module.unload()


class PluginFilters(PluginFlags):
    def __init__(self, controller):
        self.views = weakref.WeakKeyDictionary()
        self.results = {"modlist": {}, "plugins": {}}
        super().__init__(controller)

    def supports(self, plugin):
        return bool(plugin.filters)

    def evaluate(self, plugin, snapshot):
        return evaluate_filters(plugin, snapshot)

    def data_changed(self, first, last, roles=()):
        from gui_qt.runtime.plugin_columns import ColumnValuesRole
        from gui_qt.runtime.row_flags import RowFlagsRole
        if set(roles) & {ColumnValuesRole, RowFlagsRole}:
            self.schedule()
        else:
            super().data_changed(first, last, roles)

    def snapshot(self):
        result = super().snapshot()
        for panel, model in self.models.items():
            for row in result[panel]:
                row["row_flags"] = [flag.key for flag in model._row_flags.get(row["name"], ())]
                row["columns"] = {key: cell.get("value") for key, cell in
                                  model._column_values.get(row["name"], {}).items()}
        return result

    def attach(self, view, panel, key):
        if view in self.views:
            return
        self.views[view] = (panel, key)
        reference = weakref.ref(view)
        view.destroyed.connect(lambda: self.views.pop(reference(), None))
        view.plugin_filters = self
        state = self.controller.state.setdefault("filters", {})
        if not isinstance(state, dict):
            self.controller.state["filters"] = state = {}
        saved = state.get(key, {})
        view.plugin_filter_states = {k: v for k, v in saved.items() if isinstance(k, str) and v in (1, 2)} if isinstance(saved, dict) else {}
        self.refresh_view(view)

    def update(self, plugins):
        super().update(plugins)
        host = self.controller.host
        for view, (panel, key) in list(self.views.items()):
            if getattr(view, "panel_filters", None) is not None:
                native = view.panel_filters
            elif key == "modlist_tab":
                native = host._modlist_tab.filters
            else:
                native = getattr(host, "_modlist_filter_panel" if panel == "modlist" else "_plugin_filter_panel", None)
            if native is not None:
                self.attach_panel(view, native)

    def attach_panel(self, view, panel):
        if getattr(panel, "_plugin_filters_button", None) is not None:
            return
        button = QPushButton(view.tr("Plugin filters and saved views"), panel)
        panel._plugin_filters_button = button
        panel.layout().addWidget(button)
        def show():
            menu = QMenu(button)
            self.add_menu(view, menu)
            menu.exec(button.mapToGlobal(button.rect().bottomLeft()))
            menu.deleteLater()
        button.clicked.connect(show)
        panel.cleared.connect(lambda: self.clear_view(view))

    def apply_results(self, results):
        self.results = results
        for view in list(self.views):
            self.refresh_view(view)

    def refresh_view(self, view):
        if self.views[view][0] == "modlist":
            view.apply_collapse()
        else:
            view._apply_hidden()
        sync = getattr(view, "_sync_clear_action", None)
        if sync:
            sync()
        panel, key = self.views[view]
        host = self.controller.host
        if key == "modlist_tab":
            tab = getattr(host, "_modlist_tab", None)
            if tab is not None and hasattr(tab, "filter_button"):
                active = bool(self.active(view) or tab.search.text().strip() or tab.filters.any_active() or tab.conflict_names)
                tab.filter_button.setProperty("active", active)
                tab.filter_button.style().unpolish(tab.filter_button)
                tab.filter_button.style().polish(tab.filter_button)
        elif key == "plugins_tab":
            tab = getattr(host, "_plugin_panel_tabs", {}).get(0)
            if tab is not None:
                tab._sync_filters_btn()
        elif hasattr(host, "_sync_filters_btn"):
            host._sync_filters_btn("_modlist_filters_btn" if panel == "modlist" else "_plugin_filters_btn")

    def active(self, view):
        panel, _key = self.views[view]
        available = set(self.results[panel].get(None, ()))
        return {key: mode for key, mode in view.plugin_filter_states.items() if key in available}

    def hidden(self, view):
        panel, _key = self.views[view]
        active = self.active(view)
        if not active:
            return set()
        rows = self.results[panel]
        model = view.model()
        entries = model._entries if panel == "modlist" else model._rows
        def matches(name):
            matched = rows.get(name, ())
            return all((key in matched) == (mode == 1) for key, mode in active.items())
        hidden = set()
        for i, entry in enumerate(entries):
            if panel == "modlist" and entry.is_group_header:
                keep = any(matches(entries[r].name) for r in model.group_rows(entry.name))
            elif panel == "modlist" and entry.is_separator:
                keep = any(matches(entries[r].name) for r in model.sep_block_rows(i))
            else:
                keep = matches(entry.name)
            if not keep:
                hidden.add(i)
        return hidden

    def set_filter(self, view, key, mode):
        if mode:
            view.plugin_filter_states[key] = mode
        else:
            view.plugin_filter_states.pop(key, None)
        _panel, name = self.views[view]
        self.controller.state.setdefault("filters", {})[name] = dict(view.plugin_filter_states)
        self.controller.save_timer.start()
        self.refresh_view(view)

    def clear_view(self, view):
        view.plugin_filter_states.clear()
        self.set_filter(view, "", 0)

    def add_menu(self, view, menu):
        panel, _key = self.views[view]
        filters = QMenu(view.tr("Plugin filters"), menu)
        for plugin in self.plugins:
            declarations = [f for f in plugin.filters if panel in f.panels]
            if not declarations:
                continue
            group = filters.addMenu(translate(plugin, plugin.name))
            for spec in declarations:
                key = f"{plugin.id}/{spec.id}"
                item = group.addMenu(translate(plugin, spec.label))
                for mode, label in ((1, view.tr("Include matches")), (2, view.tr("Exclude matches"))):
                    action = item.addAction(label)
                    action.setCheckable(True)
                    action.setChecked(view.plugin_filter_states.get(key) == mode)
                    action.triggered.connect(lambda checked, k=key, m=mode: self.set_filter(view, k, m if checked else 0))
        if not filters.isEmpty():
            menu.addMenu(filters)
        self.add_saved_views(view, menu)

    def native_context(self, view):
        panel, name = self.views[view]
        host = self.controller.host
        if name == "modlist_tab":
            tab = host._modlist_tab
            return tab.search, tab.filters, tab.state
        if panel == "modlist":
            return host._modlist_search, host._ensure_filter_panel("_modlist_filter_panel"), host._modlist_filter_state
        return host._plugins_search, host._ensure_filter_panel("_plugin_filter_panel"), host._plugin_filter_state

    def saved_views(self, view):
        _panel, key = self.views[view]
        state = self.controller.state.setdefault("saved_views", {})
        if not isinstance(state, dict):
            self.controller.state["saved_views"] = state = {}
        if not isinstance(state.get(key), dict):
            state[key] = {}
        return state[key]

    def save_view(self, view, name):
        if name is None or not name.strip() or len(name) > 100:
            return
        name = name.strip()
        search, _panel, native = self.native_context(view)
        model, header = view.model(), view.header()
        keys = model.column_names()
        native = {k: sorted(v) if isinstance(v, (set, frozenset)) else v for k, v in native.items()}
        saved = self.saved_views(view)
        if len(saved) >= 64 and name not in saved:
            self.controller.host._notify(view.tr("At most 64 saved views are supported."), "warning")
            return
        saved[name] = {
            "query": search.text(), "native": native, "filters": dict(view.plugin_filter_states),
            "sort": list(model.sort_state()),
            "order": [keys[header.logicalIndex(i)] for i in range(model.columnCount())],
            "hidden": [key for i, key in enumerate(keys) if view.isColumnHidden(i)],
            "widths": {key: view.columnWidth(i) for i, key in enumerate(keys)},
        }
        self.controller.save_timer.start()

    def apply_view(self, view, name):
        saved = self.saved_views(view).get(name)
        if not isinstance(saved, dict):
            return
        search, native, _state = self.native_context(view)
        if native is not None:
            native.set_state(saved.get("native", {}), emit=True)
        query = saved.get("query", "")
        search.setText(query if isinstance(query, str) else "")
        states = saved.get("filters", {})
        view.plugin_filter_states = {key: mode for key, mode in states.items()
                                     if isinstance(key, str) and type(mode) is int and mode in (1, 2)} if isinstance(states, dict) else {}
        self.set_filter(view, "", 0)
        model, header = view.model(), view.header()
        keys = model.column_names()
        view._restoring = True
        try:
            for i, key in enumerate(keys):
                if i and key:
                    view.setColumnHidden(i, key in saved.get("hidden", ()))
                    width = saved.get("widths", {}).get(key, 120)
                    if type(width) is int and width > 0:
                        view.setColumnWidth(i, min(width, 2000))
            order = [key for key in saved.get("order", ()) if key in keys]
            for i, key in enumerate(order):
                header.moveSection(header.visualIndex(keys.index(key)), i)
            sort = saved.get("sort", [None, True])
            key, ascending = sort if isinstance(sort, list) and len(sort) == 2 else (None, True)
            from gui_qt.modlist.view import _COL_TO_SORTKEY as mod_keys
            from gui_qt.plugins.view import _COL_TO_SORTKEY as plugin_keys
            builtin = mod_keys if self.views[view][0] == "modlist" else plugin_keys
            col = next((i for i in range(model.columnCount()) if key and model.column_sort_key(i, builtin) == key), -1)
            model.set_sort(key if col >= 0 else None, ascending)
            header.setSortIndicator(col, Qt.AscendingOrder if ascending else Qt.DescendingOrder)
        finally:
            view._restoring = False
        view._fit_name_to_width()
        view._schedule_save()

    def add_saved_views(self, view, menu):
        saved = menu.addMenu(view.tr("Saved views"))
        for name in sorted(self.saved_views(view)):
            item = saved.addMenu(name)
            item.addAction(view.tr("Apply")).triggered.connect(lambda _checked=False, n=name: self.apply_view(view, n))
            def remove(_checked=False, n=name):
                self.saved_views(view).pop(n, None)
                self.controller.save_timer.start()
            item.addAction(view.tr("Delete")).triggered.connect(remove)
        def create():
            from gui_qt.dialogs.text_input import TextInputOverlay
            TextInputOverlay.show_over(view, view.tr("Save view"), view.tr("View name:"),
                                       lambda name: self.save_view(view, name))
        saved.addAction(view.tr("Save current view…")).triggered.connect(create)


def add_filter_menu(view, menu):
    filters = getattr(view, "plugin_filters", None)
    if filters is not None:
        filters.add_menu(view, menu)


def plugin_hidden(view):
    filters = getattr(view, "plugin_filters", None)
    return filters.hidden(view) if filters is not None else set()


def clear_plugin_filters(view):
    filters = getattr(view, "plugin_filters", None)
    if filters is not None:
        filters.clear_view(view)


def plugin_filters_active(view):
    filters = getattr(view, "plugin_filters", None)
    return bool(filters is not None and filters.active(view))
