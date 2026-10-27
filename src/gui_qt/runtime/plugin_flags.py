from __future__ import annotations

from dataclasses import replace

from PySide6.QtCore import QObject, QTimer, Signal

from amethyst_plugins.i18n import translate
from amethyst_plugins.metadata import provider_context
from amethyst_plugins.catalog import load_data, load_settings, plugin_directory
from gui_qt.runtime.row_flags import RowFlag, RowFlagsRole, set_row_flags
from gui_qt.runtime.safe_emit import safe_emit
from gui_qt.runtime.worker import LatestWorker


def evaluate_flags(plugin, snapshot):
    from gui_qt.runtime.plugin_ui import _Module
    module = _Module(replace(plugin, entrypoint=plugin.flag_provider))
    try:
        context = dict(provider_context(plugin, snapshot), plugin_id=plugin.id,
                       plugin_dir=str(plugin_directory(plugin)),
                       settings=load_settings(plugin), data=load_data(plugin))
        result = module.module.evaluate_flags(context)
        if not isinstance(result, dict) or set(result) - {"modlist", "plugins"}:
            raise ValueError("evaluate_flags must return modlist/plugins row mappings.")
        output = {}
        for panel, rows in result.items():
            if not isinstance(rows, dict):
                raise ValueError("Flag results must map row names to lists of flag IDs.")
            declared = {f.id: f for f in plugin.flags if panel in f.panels}
            known = {r["name"] for r in snapshot[panel]}
            output[panel] = {}
            for name, ids in rows.items():
                if (name not in known or not isinstance(ids, list) or len(ids) > 32
                        or any(not isinstance(fid, str) or fid not in declared for fid in ids)):
                    raise ValueError(f"Invalid row or flag ID in {panel} result: {name}")
                output[panel][name] = tuple(
                    RowFlag(f"{plugin.id}/{f.id}", str(f.icon),
                            f"{translate(plugin, plugin.name)}: {translate(plugin, f.label)}\n{translate(plugin, f.tooltip)}" if f.tooltip != f.label
                            else f"{translate(plugin, plugin.name)}: {translate(plugin, f.label)}")
                    for f in plugin.flags if f.id in ids and panel in f.panels)
        return output
    finally:
        module.retired = True
        module.unload()


class PluginFlags(QObject):
    completed = Signal(object)

    def __init__(self, controller):
        super().__init__(controller)
        self.controller = controller
        self.plugins = ()
        self.target = None
        self.generation = 0
        self.stopped = False
        self.applying = False
        self.worker = LatestWorker("plugin-flags")
        self.completed.connect(self.apply)
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.setInterval(150)
        self.timer.timeout.connect(self.refresh)
        self.poll = QTimer(self)
        self.poll.setInterval(5000)
        self.poll.timeout.connect(self.refresh)
        host = controller.host
        self.models = {"modlist": host._modlist_model, "plugins": host._plugin_model}
        for model in self.models.values():
            for signal in (model.modelReset, model.layoutChanged, model.rowsInserted,
                           model.rowsRemoved, model.rowsMoved):
                signal.connect(self.schedule)
            model.dataChanged.connect(self.data_changed)

    def current_target(self):
        state = self.controller.host._gs
        game = state.game
        return (game.game_id if game else "", str(state.profile_dir()))

    def data_changed(self, _first, _last, roles=()):
        from gui_qt.runtime.plugin_columns import ColumnValuesRole
        from gui_qt.modlist.model import HighlightRole
        from gui_qt.plugins.model import PHighlightRole
        if not roles or set(roles) - {RowFlagsRole, HighlightRole, PHighlightRole, ColumnValuesRole}:
            self.schedule()

    def schedule(self, *_args):
        if not self.stopped and not self.applying and self.plugins:
            self.generation += 1
            self.timer.start()

    def update(self, plugins):
        target = self.current_target()
        wanted = tuple(p for p in plugins if self.supports(p)) if self.controller.valid_target() else ()
        if wanted != self.plugins or target != self.target:
            self.plugins, self.target = wanted, target
            self.generation += 1
            self.worker.discard_pending()
            self.clear()
            if wanted:
                self.poll.start()
            else:
                self.poll.stop()
        self.schedule()

    def supports(self, plugin):
        return bool(plugin.flags)

    def evaluate(self, plugin, snapshot):
        return evaluate_flags(plugin, snapshot)

    def merge(self, rows, name, values):
        rows[name] = rows.get(name, ()) + values

    def apply_results(self, results):
        set_row_flags(self.models["modlist"], results["modlist"], 2)
        set_row_flags(self.models["plugins"], results["plugins"], 1)

    def snapshot(self):
        host = self.controller.host
        game = host._gs.game
        staging = game.get_effective_mod_staging_path()
        mods = self.models["modlist"]
        plugins = self.models["plugins"]
        paths = getattr(host._plugin_view, "resolved_paths", {})
        return {
            "api_version": 1, "game_id": game.game_id, "game": game.name,
            "game_path": str(game.get_game_path()), "profile": host._gs.profile,
            "profile_path": str(host._gs.profile_dir()), "mod_staging_path": str(staging),
            "modlist": [
                {"name": e.name, "display_name": e.display_name, "enabled": e.enabled,
                 "locked": e.locked, "path": str(staging / e.name), "priority": i,
                 "flags": mods._effective_flags(e.name),
                 "version": mods._versions.get(e.name, ""),
                 "category": mods._categories.get(e.name, "")}
                for i, e in enumerate(e for e in mods.natural_entries() if not e.is_separator)],
            "plugins": [
                {"name": r.name, "enabled": r.enabled, "vanilla": r.vanilla,
                 "flags": r.flags, "priority": i,
                 "path": str(paths.get(r.name.lower(), "")), "masters": list(r.masters or ())}
                for i, r in enumerate(plugins.natural_rows())],
        }

    def refresh(self):
        if (self.stopped or not self.plugins or self.controller.locked
                or getattr(self.controller.host, "_manager_plugin", None) is not None):
            return
        if self.target != self.current_target():
            self.generation += 1
            self.clear()
            self.controller.schedule_refresh()
            return
        try:
            snapshot = self.snapshot()
        except Exception as exc:
            self.clear()
            self.controller._report(f"Flag snapshot failed: {exc}")
            return
        generation, plugins = self.generation, self.plugins
        done = self.completed

        def work():
            results, errors = {"modlist": {}, "plugins": {}}, []
            if any("manager_plugins.metadata.get" in plugin.available_methods for plugin in plugins):
                try:
                    from amethyst_plugins.metadata import read_all
                    snapshot["_plugin_metadata"] = read_all(snapshot["profile_path"])
                except Exception as exc:
                    snapshot["_plugin_metadata_error"] = str(exc)
            for plugin in plugins:
                try:
                    for panel, rows in self.evaluate(plugin, snapshot).items():
                        for name, flags in rows.items():
                            self.merge(results[panel], name, flags)
                except BaseException as exc:
                    errors.append(f"{plugin.id}: {type(self).__name__} failed: {exc}")
            safe_emit(done, (generation, results, errors))

        self.worker.submit(work)

    def apply(self, result):
        generation, results, errors = result
        if (self.stopped or generation != self.generation or self.controller.locked
                or self.target != self.current_target()
                or getattr(self.controller.host, "_manager_plugin", None) is not None):
            return
        for error in errors:
            self.controller._report(error)
        self.applying = True
        try:
            self.apply_results(results)
        finally:
            self.applying = False

    def clear(self):
        self.applying = True
        try:
            self.apply_results({"modlist": {}, "plugins": {}})
        finally:
            self.applying = False

    def shutdown(self):
        self.stopped = True
        self.timer.stop()
        self.poll.stop()
        self.worker.discard_pending()
