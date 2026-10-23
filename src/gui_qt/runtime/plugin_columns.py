from __future__ import annotations

import math
from dataclasses import replace

from PySide6.QtCore import Qt

from amethyst_plugins.i18n import translate
from amethyst_plugins.metadata import provider_context
from amethyst_plugins.catalog import load_data, load_settings, plugin_directory
from gui_qt.runtime.plugin_flags import PluginFlags


ColumnValuesRole = Qt.UserRole + 61


def column_key(plugin, column):
    return f"plugin:{plugin.id}/{column.id}"


def value_key(value):
    if value is None:
        return (1, "")
    return (0, value.casefold() if isinstance(value, str) else value)


class ColumnModel:
    def init_plugin_columns(self):
        self._plugin_columns = ()
        self._column_values = {}

    def column_names(self):
        return self.builtin_columns + [key for key, _label, _column in self._plugin_columns]

    def column_label(self, section):
        if section < len(self.builtin_columns):
            return self.tr(self.builtin_columns[section])
        return self._plugin_columns[section - len(self.builtin_columns)][1]

    def column_sort_key(self, section, builtin):
        if section is None or not 0 <= section < self.columnCount():
            return None
        return builtin.get(section, self.column_names()[section]
                           if section >= len(self.builtin_columns) else None)

    def set_plugin_columns(self, columns):
        columns = tuple(columns)
        if columns == self._plugin_columns:
            return
        self.beginResetModel()
        self._plugin_columns = columns
        self._column_values = {}
        if (self._sort_key or "").startswith("plugin:"):
            if self._sort_key not in self.column_names():
                self._sort_key = None
        if hasattr(self, "_entries"):
            self._entries = self._derive_display()
        else:
            self._rows = self._derive_display()
        self.endResetModel()
        self.plugin_columns_changed.emit()

    def plugin_column_data(self, name, section, role):
        key, label, column = self._plugin_columns[section - len(self.builtin_columns)]
        cell = self._column_values.get(name, {}).get(key, {})
        value = cell.get("value")
        if role == Qt.DisplayRole:
            if value is None:
                return ""
            if column.type == "boolean":
                return self.tr("Yes") if value else self.tr("No")
            return str(value)
        if role == Qt.ToolTipRole:
            return cell.get("tooltip", label)
        if role == Qt.TextAlignmentRole:
            return int(Qt.AlignVCenter | (Qt.AlignRight if column.type == "number" else Qt.AlignLeft))
        return None

    def set_column_values(self, values):
        if self._column_values == values:
            return
        self._column_values = values
        if (self._sort_key or "").startswith("plugin:"):
            self._rebuild_display()
        if self.rowCount() and self._plugin_columns:
            self.dataChanged.emit(self.index(0, len(self.builtin_columns)),
                                  self.index(self.rowCount() - 1, self.columnCount() - 1),
                                  [ColumnValuesRole])


def evaluate_columns(plugin, snapshot):
    from gui_qt.runtime.plugin_ui import _Module
    module = _Module(replace(plugin, entrypoint=plugin.column_provider))
    try:
        context = dict(provider_context(plugin, snapshot), plugin_id=plugin.id,
                       plugin_dir=str(plugin_directory(plugin)),
                       settings=load_settings(plugin), data=load_data(plugin))
        result = module.module.evaluate_columns(context)
        if not isinstance(result, dict) or set(result) - {"modlist", "plugins"}:
            raise ValueError("evaluate_columns must return modlist/plugins row mappings.")
        output = {}
        for panel, rows in result.items():
            if not isinstance(rows, dict):
                raise ValueError("Column results must map row names to cell mappings.")
            known = {row["name"] for row in snapshot[panel]}
            declared = {c.id: c for c in plugin.columns if panel in c.panels}
            output[panel] = {}
            for name, cells in rows.items():
                if name not in known or not isinstance(cells, dict) or set(cells) - declared.keys():
                    raise ValueError(f"Invalid row or column ID: {name}")
                output[panel][name] = {}
                for cid, cell in cells.items():
                    cell = cell if isinstance(cell, dict) else {"value": cell}
                    value, tooltip = cell.get("value"), cell.get("tooltip", "")
                    kind = declared[cid].type
                    valid = (value is None or kind == "text" and isinstance(value, str) and len(value) <= 4000
                             or kind == "number" and type(value) in (int, float) and math.isfinite(value)
                             or kind == "boolean" and type(value) is bool)
                    if (not valid or set(cell) - {"value", "tooltip"}
                            or not isinstance(tooltip, str) or len(tooltip) > 4000):
                        raise ValueError(f"Invalid {kind} cell: {name}/{cid}")
                    output[panel][name][column_key(plugin, declared[cid])] = dict(cell)
        return output
    finally:
        module.retired = True
        module.unload()


class PluginColumns(PluginFlags):
    def supports(self, plugin):
        return bool(plugin.columns)

    def evaluate(self, plugin, snapshot):
        return evaluate_columns(plugin, snapshot)

    def merge(self, rows, name, values):
        rows.setdefault(name, {}).update(values)

    def update(self, plugins):
        plugins = tuple(plugins) if self.controller.valid_target() else ()
        for panel, model in self.models.items():
            model.set_plugin_columns(
                (column_key(p, c), f"{translate(p, p.name)}: {translate(p, c.label)}", c)
                for p in plugins for c in p.columns if panel in c.panels)
        super().update(plugins)

    def apply_results(self, results):
        for panel, model in self.models.items():
            model.set_column_values(results[panel])
