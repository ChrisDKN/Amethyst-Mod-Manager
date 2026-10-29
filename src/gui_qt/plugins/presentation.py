from copy import copy

from PySide6.QtCore import Signal

from gui_qt.plugins.model import PluginModel, PHighlightRole


class PluginPresentation(PluginModel):
    refreshed = Signal()

    def __init__(self, source, parent):
        super().__init__()
        self.setParent(parent)
        self.source = source
        self._publishing = False
        self._connections = []
        self._sort_key, self._sort_ascending = source.sort_state()
        self.sync_from_source()
        for signal in (source.modelReset, source.layoutChanged, source.rowsMoved,
                       source.rowsInserted, source.rowsRemoved, source.dataChanged):
            signal.connect(self.sync_from_source)
            self._connections.append(signal)

    def sync_from_source(self, *_args):
        if self._publishing:
            return
        if len(_args) == 3 and _args[2] and set(_args[2]) == {PHighlightRole}:
            return
        source = self.source
        context_changed = source._game is not self._game or source._profile_dir != self._profile_dir
        self.set_plugin_columns(source._plugin_columns)
        for field in ("_game", "_profile", "_profile_dir", "_master_block"):
            setattr(self, field, getattr(source, field))
        for field in ("_locks", "_ul_groups", "_row_flags", "_column_values"):
            setattr(self, field, copy(getattr(source, field)))
        old = {row.name.lower(): row for row in self._natural}
        rows = []
        for row in source.natural_rows():
            local = old.get(row.name.lower())
            if local is None:
                local = copy(row)
            else:
                local.__dict__.update(row.__dict__)
            rows.append(local)
        reset = context_changed or set(old) != {row.name.lower() for row in rows}
        if reset:
            self.beginResetModel()
        self._natural = rows
        self._refresh_natural_caches()
        if reset:
            self._rows = self._derive_display()
            self.endResetModel()
        else:
            self._rebuild_display()
        if self.rowCount():
            self.dataChanged.emit(self.index(0, 0),
                                  self.index(self.rowCount() - 1, self.columnCount() - 1))
        self.refreshed.emit()

    def toggle_lock(self, i):
        name = self.row(i).name.lower()
        index = next((i for i, row in enumerate(self.source._rows)
                      if row.name.lower() == name), None)
        if index is not None:
            self.source.toggle_lock(index)

    def _save(self):
        self._publishing = True
        try:
            source = self.source
            old = {row.name.lower(): row for row in source.natural_rows()}
            rows = []
            for row in self._natural:
                authoritative = old[row.name.lower()]
                authoritative.__dict__.update(row.__dict__)
                rows.append(authoritative)
            source._natural = rows
            source._refresh_natural_caches()
            source._rebuild_display()
            if source.rowCount():
                source.dataChanged.emit(source.index(0, 0),
                                        source.index(source.rowCount() - 1, source.columnCount() - 1))
            source._save()
        finally:
            self._publishing = False
            self.sync_from_source()

    def dispose(self):
        for signal in self._connections:
            signal.disconnect(self.sync_from_source)
        self._connections.clear()
