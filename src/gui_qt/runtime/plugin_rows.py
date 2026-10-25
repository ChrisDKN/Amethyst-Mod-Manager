from copy import deepcopy
import json
import re
import weakref

from PySide6.QtCore import QObject, QThread, QTimer
from PySide6.QtWidgets import QApplication, QMenu

from amethyst_plugins.i18n import translate
from gui_qt.modlist.external import ExternalModEntry, external


class PluginRows(QObject):
    def __init__(self, controller):
        super().__init__(controller)
        self.controller = controller
        self.plugins = {}
        self.sections = {}
        self.target = self.current_target()
        QApplication.instance()._amethyst_plugin_rows = self

    def current_target(self):
        state = self.controller.host._gs
        return (state.game.game_id if state.game else '', state.profile)

    def update(self, plugins):
        target = self.current_target()
        self.plugins = {p.id: p for p in plugins if p.modlist_sections}
        if target != self.target or not self.controller.valid_target():
            self.sections.clear()
        self.target = target
        for key, record in list(self.sections.items()):
            plugin = self.plugins.get(key[0])
            if (plugin != record[1] or record[0]() is None
                    or not any(s.id == key[1] for s in plugin.modlist_sections)):
                self.sections.pop(key)
        self.apply()

    def publish(self, owner, plugin, section_id, rows, *, game_id, profile, on_action=None):
        if QThread.currentThread() != self.thread():
            raise RuntimeError('Publish modlist rows on the GUI thread.')
        target = (game_id, profile)
        if (self.controller.stopped or not owner.active or target != self.current_target()
                or target != self.target or plugin.id not in self.plugins):
            return False
        section = next((s for s in plugin.modlist_sections if s.id == section_id), None)
        if section is None or on_action is not None and not callable(on_action):
            raise ValueError('Declare the section and provide a callable action handler.')
        key = (plugin.id, section_id)
        previous = self.sections.get(key)
        if previous is not None and previous[0]() is not owner:
            raise ValueError('This section is already owned by another plugin context.')
        if rows is None:
            self.sections.pop(key, None)
        else:
            if not isinstance(rows, list) or len(rows) > 10000:
                raise ValueError('Expected at most 10000 read-only rows per section.')
            if len(json.dumps(rows, allow_nan=False).encode()) > 8 * 1024 * 1024:
                raise ValueError('Modlist section data exceeds 8 MiB.')
            ids = set()
            for row in rows:
                if not isinstance(row, dict) or set(row) - {'id', 'name', 'tooltip', 'values'}:
                    raise ValueError('Invalid read-only row.')
                rid, name = row.get('id'), row.get('name')
                values = row.get('values', {})
                tooltip = row.get('tooltip', '')
                if (not isinstance(rid, str) or not re.fullmatch(r'[a-zA-Z0-9_.:-]{1,128}', rid)
                        or rid in ids or not isinstance(name, str) or not name.strip() or len(name) > 1000
                        or not isinstance(tooltip, str) or len(tooltip) > 8000
                        or not isinstance(values, dict)
                        or set(values) - {'category', 'version', 'author', 'size', 'installed', 'updated'}
                        or any(not isinstance(v, str) or len(v) > 4000 for v in values.values())):
                    raise ValueError('Invalid read-only row ID, name, tooltip, or values.')
                ids.add(rid)
            self.sections[key] = (weakref.ref(owner), plugin, section, deepcopy(rows), on_action, target)
        self.apply()
        return True

    def apply(self):
        entries = []
        for (pid, sid), (_owner, plugin, section, rows, _callback, _target) in sorted(self.sections.items()):
            prefix = f'@plugin:{pid}/{sid}/'
            label = f'{translate(plugin, plugin.name)}: {translate(plugin, section.label)}'
            entries.append(ExternalModEntry(prefix, True, True, True, label=label,
                plugin_id=pid, section_id=sid, tooltip='Read-only entries managed by a plugin.'))
            entries.extend(ExternalModEntry(prefix + row['id'], True, True,
                label=row['name'], plugin_id=pid, section_id=sid, item_id=row['id'],
                tooltip=row.get('tooltip', ''), values=dict(row.get('values', {}))) for row in rows)
        host = self.controller.host
        if host._modlist_model.set_external_entries(entries):
            for name in ('_apply_modlist_search', '_apply_modlist_filters'):
                refresh = getattr(host, name, None)
                if callable(refresh):
                    refresh()

    def release(self, owner):
        for key, record in list(self.sections.items()):
            if record[0]() is owner or record[0]() is None:
                self.sections.pop(key)
        self.apply()

    def run(self, key, action_id, ids, target):
        if (self.controller.stopped or self.controller.locked or target != self.current_target()
                or getattr(self.controller.host, '_manager_plugin', None) is not None
                or not self.controller.valid_target()):
            return
        record = self.sections.get(key)
        if record is None:
            return
        owner, _plugin, section, rows, callback, scope = record
        known = {row['id'] for row in rows}
        if (owner() is None or not owner().active or scope != target or not callable(callback)
                or not any(a[0] == action_id for a in section.actions) or not set(ids) <= known):
            return
        try:
            callback({'action_id': action_id, 'section_id': key[1], 'ids': list(ids),
                      'game_id': target[0], 'profile': target[1]})
        except Exception as exc:
            self.controller._report(f'{key[0]}: section action failed: {exc}')

    def menu(self, view, index):
        model = view.model()
        entry = model.entry(index.row())
        key = (entry.plugin_id, entry.section_id)
        record = self.sections.get(key)
        menu = QMenu(view)
        if record is None or self.controller.locked or not view.isEnabled():
            return menu
        _owner, plugin, section, rows, callback, target = record
        selected = {i.row() for i in view.selectionModel().selectedRows()}
        if index.row() not in selected:
            selected = {index.row()}
        ids = [model.entry(r).item_id for r in sorted(selected)
               if external(model.entry(r)) and not model.entry(r).is_separator
               and (model.entry(r).plugin_id, model.entry(r).section_id) == key]
        for aid, label, selection in section.actions:
            if selection == 'items' and (entry.is_separator or not ids):
                continue
            action = menu.addAction(translate(plugin, label).replace('&', '&&'))
            action.setEnabled(callable(callback))
            def trigger(_checked=False, a=aid, items=tuple(ids) if selection == 'items' else ()):
                menu.close()
                QTimer.singleShot(0, self, lambda: self.run(key, a, items, target))
            action.triggered.connect(trigger)
        return menu


def external_menu(view, index):
    runtime = getattr(QApplication.instance(), '_amethyst_plugin_rows', None)
    return runtime.menu(view, index) if runtime is not None else QMenu(view)
