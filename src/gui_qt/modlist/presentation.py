"""An independently sorted modlist presentation with one authoritative writer."""

from copy import copy, deepcopy

from PySide6.QtCore import Signal

from gui_qt.modlist.model import ModListModel, HighlightRole


class ModListPresentation(ModListModel):
    refreshed = Signal()
    _DATA_FIELDS = (
        "_external_entries",
        "_column_values", "_versions", "_installed", "_updated", "_categories", "_authors",
        "_descriptions", "_sizes", "_size_bytes", "_nexus_mod_ids",
        "_nexus_file_ids", "_content", "_conflicts", "_bsa_conflicts",
        "_uuid_conflicts", "_flags", "_row_flags", "_mod_links", "_notes", "_modified_mf", "_prertx_mods",
        "_root_rule_mods", "_skse_issues", "_rerun_fomod_mods", "_sep_locks",
        "_sep_colors", "_sep_deploy", "_boundary_counts",
    )

    def __init__(self, source, parent=None):
        super().__init__()
        self.setParent(parent)
        self.source = source
        self._publishing = False
        self._syncing = False
        self._data_pending = False
        self._last_save_succeeded = True
        self._connections = []
        self._collapsed = set(source._collapsed)
        self._sort_key, self._sort_ascending = source.sort_state()
        self._default_priority_reversed = source.default_priority_reversed
        self.sync_from_source()
        for signal in (source.modelReset, source.layoutChanged,
                       source.rowsMoved, source.rowsInserted, source.rowsRemoved,
                       source.groups_changed, source.shared_state_changed):
            signal.connect(self.sync_from_source)
            self._connections.append((signal, self.sync_from_source))
        source.dataChanged.connect(self._source_data_changed)
        self._connections.append((source.dataChanged, self._source_data_changed))
        source.data_batch_finished.connect(self._flush_data_changes)
        self._connections.append((source.data_batch_finished, self._flush_data_changes))
        self.enabled_changed.connect(self._forward_enabled)
        self.save_failed.connect(source.save_failed.emit)

    def dispose(self):
        for signal, callback in self._connections:
            signal.disconnect(callback)
        self._connections.clear()

    def _source_data_changed(self, _first, _last, roles=()):
        if roles and set(roles) == {HighlightRole}:
            return
        if self.source._data_batch_depth:
            self._data_pending = True
            return
        self.sync_from_source()

    def _flush_data_changes(self):
        if self._data_pending:
            self.sync_from_source()

    def _forward_enabled(self, changes):
        if self._last_save_succeeded:
            self.source.enabled_changed.emit(changes)

    def sync_from_source(self, *_args):
        if self._publishing or self._syncing:
            return
        self._data_pending = False
        self._syncing = True
        try:
            source = self.source
            self.set_plugin_columns(source._plugin_columns)
            for field in self._DATA_FIELDS:
                setattr(self, field, copy(getattr(source, field)))
            groups = deepcopy(source._mod_groups)
            for name, group in groups.items():
                group["collapsed"] = self._mod_groups.get(name, group)["collapsed"]
            groups_changed = groups != self._mod_groups
            old = {entry.name: entry for entry in self._natural}
            entries = []
            for entry in source._natural:
                local = old.get(entry.name)
                if local is None:
                    local = copy(entry)
                else:
                    local.__dict__.update(entry.__dict__)
                entries.append(local)
            self._natural = entries
            self._mod_groups = groups
            self._saved_mod_groups = deepcopy(groups)
            self._baseline_names = set(source._baseline_names)
            self.modlist_path = source.modlist_path
            self._index_groups()
            if len(self._derive_display()) != len(self._entries):
                self.beginResetModel()
                self._entries = self._derive_display()
                self.endResetModel()
            else:
                self._rebuild_display(force=groups_changed)
            if self.rowCount():
                self.dataChanged.emit(self.index(0, 0),
                                      self.index(self.rowCount() - 1, self.columnCount() - 1))
            self.refreshed.emit()
        finally:
            self._syncing = False

    def _publish(self):
        source = self.source
        groups = deepcopy(self._mod_groups)
        for name, group in groups.items():
            group["collapsed"] = source._mod_groups.get(name, group)["collapsed"]
        old = {entry.name: entry for entry in source._natural}
        entries = []
        for entry in self._natural:
            authoritative = old.get(entry.name)
            if authoritative is None:
                authoritative = copy(entry)
            else:
                authoritative.__dict__.update(entry.__dict__)
            entries.append(authoritative)
        source._publish_group_edit(entries, groups)
        if source.rowCount():
            source.dataChanged.emit(source.index(0, 0),
                                    source.index(source.rowCount() - 1, source.columnCount() - 1))

    def save(self, edit_ctx=None, *, guard_mods=None):
        self._publishing = True
        self._last_save_succeeded = False
        source = self.source
        previous = ([copy(e) for e in source._natural], deepcopy(source._mod_groups))
        try:
            self._publish()
            saved = source.save(edit_ctx, guard_mods=guard_mods)
            self._last_save_succeeded = saved
            if not saved and not source._group_recovery_needed:
                source._publish_group_edit(*previous)
            return saved
        finally:
            self._publishing = False
            self.sync_from_source()

    def _save_groups(self):
        self._publishing = True
        previous = ([copy(e) for e in self.source._natural],
                    deepcopy(self.source._mod_groups))
        try:
            self._publish()
            self.source._save_groups()
            self._saved_mod_groups = deepcopy(self._mod_groups)
            self.source.groups_changed.emit()
        except Exception:
            self.source._publish_group_edit(*previous)
            raise
        finally:
            self._publishing = False

    def toggle_group(self, leader):
        if leader not in self._mod_groups:
            return False
        self._mod_groups[leader]["collapsed"] = not self.is_group_collapsed(leader)
        self.groups_changed.emit()
        return True

    def set_groups_collapsed(self, collapsed):
        for group in self._mod_groups.values():
            group["collapsed"] = collapsed
        self.groups_changed.emit()

    def publish_separator_locks(self):
        self.source._sep_locks = dict(self._sep_locks)
        self.source.shared_state_changed.emit()

    def set_version(self, name, version):
        self.source.set_version(name, version)
