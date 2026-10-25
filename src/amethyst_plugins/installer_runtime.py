from __future__ import annotations

import importlib.util
from Utils import json_limits as json
from pathlib import Path
import sys
import uuid

from .catalog import load_data, load_settings, plugin_directory
from .installers import InstallPlan, _identity, _relative


class InstallSession:
    def __init__(self, plan, prepared, log, progress, cancel):
        self.plan = plan
        self.name = f"_amethyst_installer_{uuid.uuid4().hex}"
        self.state = json.loads(plan.state)
        self.module = None
        work = plan.root / ".plugin-install"
        work.mkdir()
        self.output_dir = work / "output"
        self.context = {
            "api_version": 1, "plugin_id": plan.plugin.id, "installer_id": plan.installer.id,
            "plugin_dir": str(plugin_directory(plan.plugin)),
            "settings": load_settings(plan.plugin), "data": load_data(plan.plugin),
            "game_id": prepared.game.game_id, "game": prepared.game.name,
            "game_handler": prepared.game, "profile": prepared.profile_dir.name,
            "profile_path": str(prepared.profile_dir), "mod_name": prepared.mod_name,
            "archive_path": str(prepared.archive), "extract_dir": str(plan.root),
            "work_dir": str(work), "output_dir": str(self.output_dir),
            "cancel": cancel if cancel is not None else plan.cancel, "check_cancelled": self.check_cancelled,
            "log": lambda message: log(f"{plan.plugin.name}: {message}"), "progress": progress,
        }

    def check_cancelled(self):
        if self.context["cancel"] is not None and self.context["cancel"].is_set():
            from Utils.operations import OperationCancelled
            raise OperationCancelled("Plugin installation cancelled.")
        self.plan.check_cancelled()

    def load(self):
        path = self.plan.installer.entrypoint
        spec = importlib.util.spec_from_file_location(self.name, path,
            submodule_search_locations=[str(path.parent)])
        self.module = importlib.util.module_from_spec(spec)
        sys.modules[self.name] = self.module
        spec.loader.exec_module(self.module)

    def validate(self, phase, *, replacing=False):
        self.check_cancelled()
        function = getattr(self.module, "validate_install", None)
        if function is not None:
            function(self.context, self.state, phase, replacing=replacing)

    def commit(self):
        self.check_cancelled()
        function = getattr(self.module, "commit_install", None)
        if function is not None:
            function(self.context, self.state)

    def rollback(self):
        function = getattr(self.module, "rollback_install", None)
        if function is not None:
            try:
                function(self.context, self.state)
            except Exception as exc:
                self.context["log"](f"Installer rollback failed: {exc}")

    def close(self):
        for key in list(sys.modules):
            if key == self.name or key.startswith(self.name + "."):
                sys.modules.pop(key, None)


def run_interactive(plan, prepared, log, progress=None, cancel=None):
    plan.validate(prepared.game.game_id)
    session = InstallSession(plan, prepared, log, progress, cancel)
    try:
        session.load()
        session.check_cancelled()
        result = session.module.run_install(session.context, session.state)
        session.check_cancelled()
        if not isinstance(result, dict) or set(result) != {"files"}:
            raise ValueError("run_install must return an object containing files.")
        rows = result["files"]
        if not isinstance(rows, list) or not 1 <= len(rows) <= 200000:
            raise ValueError("Plugin output requires between 1 and 200000 files.")
        root = session.output_dir
        if root.resolve() != root.absolute() or not root.is_dir():
            raise ValueError("Plugin output must be a directory without symlinks.")
        files, identities, destinations = [], [], set()
        for row in rows:
            session.check_cancelled()
            if not isinstance(row, dict) or set(row) != {"source", "destination"}:
                raise ValueError("Plugin output files require source and destination paths.")
            source, destination = _relative(row["source"]), _relative(row["destination"])
            path = root / source
            if path.resolve() != path.absolute():
                raise ValueError("Plugin output cannot contain symlinks.")
            identity = _identity(path)
            key = destination.casefold()
            if key == "meta.ini" or key in destinations:
                raise ValueError(f"Plugin output destinations conflict: {destination}")
            destinations.add(key)
            files.append((source, destination, False))
            identities.append(identity)
        for destination in destinations:
            parts = destination.split("/")
            if any("/".join(parts[:index]) in destinations for index in range(1, len(parts))):
                raise ValueError("A plugin output destination is both a file and a directory.")
        session.validate("ready")
        concrete = InstallPlan(plan.plugin, plan.installer, root, tuple(files), tuple(identities),
            plan.archive, plan.archive_identity, plan.signature, plan.revision, session.context["cancel"])
        concrete.validate(prepared.game.game_id)
        return concrete, session
    except BaseException:
        try:
            session.rollback()
        finally:
            session.close()
        raise
