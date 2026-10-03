from __future__ import annotations

import json
from pathlib import Path

from PySide6.QtCore import QObject, QTimer, Signal

from Utils.fomod.choices import load_choices
from Utils.fomod.session import FomodReinstallRequest


def target_identity(path):
    info = Path(path).stat()
    return info.st_dev, info.st_ino


class FomodChoicesController(QObject):
    changed = Signal(object, bool, str)
    _checked = Signal(object)
    _finished = Signal(object, bool, str)

    def __init__(self, app):
        super().__init__(app)
        self.app = app
        self.drafts = {}
        self.pending = {}
        self.executing = set()
        self.epoch = 0
        self._checked.connect(self._on_checked)
        self._finished.connect(self.finish)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self.refresh)
        for name in ("_plugin_model", "_modlist_model"):
            model = getattr(app, name, None)
            if model is not None:
                for signal_name in ("modelReset", "dataChanged", "layoutChanged",
                                    "rowsInserted", "rowsRemoved"):
                    getattr(model, signal_name).connect(self.schedule_refresh)

    def schedule_refresh(self, *_args):
        self._timer.start(250)

    def is_pending(self, key):
        return key in self.pending

    def owner(self, mod, profile):
        from Utils.profiles.groups import entry_owner_profile, is_group
        profile = Path(profile)
        if is_group(profile):
            owner = entry_owner_profile(profile, mod)
            if owner is None:
                raise ValueError("This mod no longer belongs to the profile group.")
            return Path(owner[0]), owner[1]
        return profile, mod

    def loader(self, mod, profile, game_name):
        app = self.app
        game = app._gs.game
        if (game is None or game.name != game_name
                or str(app._gs.profile_dir()) != profile):
            return lambda: (None, None)
        try:
            owner, folder = self.owner(mod, profile)
            from Utils.mods.copy import resolve_target_staging
            mod_dir = Path(resolve_target_staging(game, owner)) / folder
        except (OSError, ValueError):
            return lambda: (None, None)
        live = self.live_plugins()

        def load():
            choices = load_choices(folder, owner, game_name, mod_dir=mod_dir)
            context = self.context(game, Path(profile),
                                   choices.config if choices else None, live)
            return choices, context

        return load

    def mod_renamed(self, game_name, profile, old_name, new_name):
        old_key = game_name, str(profile), old_name
        new_key = game_name, str(profile), new_name
        request = self.pending.get(old_key)
        if request is not None:
            self.finish(request, False, self.tr("The mod was renamed. Review the choices and retry."))
        view = self.app._tabs.content_for_key("fomod_choices")
        showing = view is not None and view._key() == old_key
        if showing:
            view._store_draft()
        if old_key in self.drafts:
            self.drafts[new_key] = self.drafts.pop(old_key)
        if showing:
            view.set_mod(new_name)
            self.drafts.pop(old_key, None)
            self.app._tabs.set_tab_title("fomod_choices", self.tr("FOMOD: {0}").format(new_name))

    def live_plugins(self):
        model = getattr(self.app, "_plugin_model", None)
        if model is None:
            return set(), set()
        return set(model.all_lower()), set(model.enabled_lower())

    @staticmethod
    def context(game, profile, config, live):
        from Utils.mods.install import _collection_plugin_context, _fomod_needs_loose_file_context
        installed, active, loose = _collection_plugin_context(
            game, profile, include_loose_files=(config is not None
                                               and _fomod_needs_loose_file_context(config)))
        live_all, live_active = live
        return (installed | live_all, (active | live_active) - (live_all - live_active), loose)

    def refresh(self):
        app = self.app
        if (getattr(app, "_install_running", False)
                or getattr(app, "_deploy_running", False)
                or getattr(app, "_col_install_running", False)
                or getattr(app, "_bsa_op_running", False)
                or getattr(app, "_staged_finish_running", False)):
            self._timer.start(1000)
            return
        view = app._tabs.content_for_key("fomod_choices")
        if view is not None and view.isVisible():
            if not view.refresh_context():
                self._timer.start(1000)

    def profile_changed(self):
        self.epoch += 1
        for request in list(self.pending.values()):
            if request not in self.executing:
                self.finish(request, False, self.tr(
                    "The active game or profile changed. Return to the original profile and retry."))
        view = self.app._tabs.content_for_key("fomod_choices")
        if view is not None:
            self.app._tabs.close_tab("fomod_choices")

    def submit(self, key, session, config_id):
        if key in self.pending or session.errors:
            return
        app = self.app
        game_name, profile, mod = key
        try:
            owner, folder = self.owner(mod, profile)
            from Utils.mods.copy import resolve_target_staging
            target = Path(resolve_target_staging(app._gs.game, owner)) / folder
            request = FomodReinstallRequest(
                game_name, profile, mod, str(owner), folder, config_id,
                json.dumps(session.selections), str(target), target_identity(target), self.epoch)
        except Exception as exc:
            self.changed.emit(key, False, str(exc))
            return
        self.pending[key] = request
        self.changed.emit(key, False, "")
        loader = self.loader(mod, profile, game_name)

        def check():
            try:
                choices, context = loader()
                request.validate(choices.config if choices else None, context)
                return request, ""
            except Exception as exc:
                return request, str(exc)

        from gui_qt.worker import run_in_worker
        run_in_worker(check, self._checked, name="fomod-reinstall-check",
                      error_result=(request, self.tr("Could not check FOMOD choices.")))

    def _on_checked(self, result):
        request, error = result
        if error or not self.valid_target(request):
            if error:
                self.finish(request, False, error)
            return
        try:
            started = self.app._reinstall_mods([request.mod_name], fomod_request=request)
        except Exception as exc:
            self.finish(request, False, str(exc))
            return
        if not started:
            self.finish(request)

    def valid_target(self, request):
        if self.pending.get(request.key) is not request:
            return False
        app = self.app
        try:
            valid = (self.epoch == request.epoch
                     and app._gs.game is not None
                     and app._gs.game.name == request.game_name
                     and str(app._gs.profile_dir()) == request.profile_dir
                     and self.owner(request.mod_name, request.profile_dir)
                     == (Path(request.owner_profile_dir), request.owner_mod_name)
                     and target_identity(request.target_dir) == request.target_identity)
        except (OSError, ValueError):
            valid = False
        if not valid:
            self.finish(request, False, self.tr(
                "The game, profile, or installed mod changed. Review the choices and try again."))
        return valid

    def finish(self, request, success=False, message=""):
        if request is None or self.pending.get(request.key) is not request:
            return
        self.pending.pop(request.key, None)
        self.executing.discard(request)
        if success:
            self.drafts.pop(request.key, None)
        message = message or (self.tr("Reinstalled with these choices.") if success else
                              self.tr("Reinstall did not complete. Your edited choices are kept."))
        self.app._append_log(f"[fomod] {request.mod_name}: {message}")
        self.changed.emit(request.key, success, message)

    def finish_from_worker(self, request, message):
        self._finished.emit(request, False, message)
