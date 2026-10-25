from __future__ import annotations

import hashlib
import importlib.util
from Utils import json_limits as json
import os
import stat
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path

from .catalog import discover, load_data, load_settings, plugin_directory


def _identity(path):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode):
        raise ValueError("Installer source files must be regular files.")
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)


def _relative(value):
    if (not isinstance(value, str) or len(value) > 4096 or "\\" in value or ":" in value
            or any(ord(char) < 32 for char in value)
            or any(part in ("", ".", "..") for part in value.split("/"))):
        raise ValueError("Installer paths must be safe, nonempty archive-relative file paths.")
    return value


def _signature(plugin, installer):
    return [repr(plugin), hashlib.sha256(installer.entrypoint.read_bytes()).hexdigest(),
            load_settings(plugin), load_data(plugin)]


@dataclass(frozen=True)
class InstallPlan:
    interactive = False

    plugin: object
    installer: object
    root: Path
    files: tuple
    identities: tuple
    archive: Path
    archive_identity: tuple
    signature: str
    revision: str
    cancel: object = None

    def check_cancelled(self):
        if self.cancel is not None and self.cancel.is_set():
            from Utils.operations import OperationCancelled
            raise OperationCancelled("Plugin installation cancelled.")

    def validate(self, game_id):
        self.check_cancelled()
        plugins, _errors = discover(game_id)
        if self.plugin not in plugins or self.signature != json.dumps(_signature(self.plugin, self.installer), sort_keys=True):
            raise ValueError("The plugin installer changed; inspect the archive again.")
        if _identity(self.archive) != self.archive_identity:
            raise ValueError("The archive changed after its installer was prepared.")
        for (source, _destination, _folder), identity in zip(self.files, self.identities):
            self.check_cancelled()
            path = self.root / source
            if path.resolve() != path.absolute() or _identity(path) != identity:
                raise ValueError("An extracted installer source changed; inspect the archive again.")

    def summary(self):
        return {"type": "plugin", "supported": True, "choices_required": False,
                "plugin_id": self.plugin.id, "installer_id": self.installer.id,
                "label": self.installer.label, "revision": self.revision,
                "file_count": len(self.files), "total_bytes": sum(info[2] for info in self.identities),
                "files": [{"source": source, "destination": destination} for source, destination, _folder in self.files[:100]],
                "truncated": len(self.files) > 100}


@dataclass(frozen=True)
class InteractiveInstallPlan(InstallPlan):
    interactive = True
    state: str = "{}"

    def summary(self):
        return {"type": "plugin", "supported": False, "choices_required": True,
                "requires_desktop": True, "plugin_id": self.plugin.id,
                "installer_id": self.installer.id, "label": self.installer.label,
                "revision": self.revision,
                "reason": "This plugin installer requires its desktop installer window."}

    def run(self, prepared, log, progress=None, cancel=None):
        from .installer_runtime import run_interactive
        return run_interactive(self, prepared, log, progress, cancel)


def prepare_installer(prepared, log, progress=None, cancel=None, *, kind="plain"):
    plugins, errors = discover(prepared.game.game_id)
    for error in errors:
        log(f"Plugin discovery: {error}")
    candidates = [(p, installer) for p in plugins for installer in p.installers
                  if kind in installer.formats and (not installer.extensions
                  or prepared.archive.name.lower().endswith(installer.extensions))]
    if not candidates:
        return None
    candidates.sort(key=lambda item: (-item[1].priority, item[0].id, item[1].id))
    root = prepared.extract_dir.resolve()
    files = {}

    def check_cancelled():
        if cancel is not None and cancel.is_set():
            from Utils.operations import OperationCancelled
            raise OperationCancelled("Plugin installer inspection cancelled.")

    for directory, folders, names in os.walk(root, followlinks=False):
        check_cancelled()
        folders[:] = sorted(name for name in folders if not (Path(directory) / name).is_symlink())
        for name in sorted(names):
            path = Path(directory) / name
            if path.is_symlink() or not path.is_file():
                continue
            files[path.relative_to(root).as_posix()] = _identity(path)
            if len(files) > 200000:
                raise ValueError("Plugin installer inventory exceeds 200000 files.")
    folded = {path.casefold() for path in files}
    from .metadata import provider_context
    for plugin, installer in candidates:
        if any(path.casefold() not in folded for path in installer.required_files):
            continue
        settings = load_settings(plugin)
        if settings.get("enabled") is False:
            continue
        check_cancelled()
        name = f"_amethyst_installer_{uuid.uuid4().hex}"
        try:
            spec = importlib.util.spec_from_file_location(name, installer.entrypoint,
                                                         submodule_search_locations=[str(installer.entrypoint.parent)])
            module = importlib.util.module_from_spec(spec)
            sys.modules[name] = module
            spec.loader.exec_module(module)
            context = {"api_version": 1, "plugin_id": plugin.id, "installer_id": installer.id,
                       "plugin_dir": str(plugin_directory(plugin)), "settings": settings, "data": load_data(plugin),
                       "game_id": prepared.game.game_id, "game": prepared.game.name,
                       "profile": prepared.profile_dir.name, "profile_path": str(prepared.profile_dir),
                       "archive_path": str(prepared.archive), "extract_dir": str(root), "mod_name": prepared.mod_name,
                       "installer_kind": kind,
                       "files": [{"path": path, "size_bytes": identity[2]} for path, identity in files.items()],
                       "check_cancelled": check_cancelled, "log": lambda message: log(f"{plugin.name}: {message}")}
            context["metadata"] = provider_context(plugin, {"profile_path": str(prepared.profile_dir)})["metadata"]
            if progress is not None:
                progress(0, 0, f"Inspecting installer: {installer.label}")
            result = module.inspect_install(context)
            check_cancelled()
            if result is None:
                continue
            if isinstance(result, dict) and result.get("interactive") is True:
                if (not installer.interactive or set(result) != {"interactive", "state"}
                        or not isinstance(result["state"], dict)):
                    raise ValueError("Interactive recognition requires a declared interactive installer and state.")
                state = json.dumps(result["state"], max_bytes=1024 * 1024, sort_keys=True)
                signature = json.dumps(_signature(plugin, installer), sort_keys=True)
                archive_identity = _identity(prepared.archive)
                revision = hashlib.sha256(json.dumps([signature, archive_identity, state,
                    [(path, info[2]) for path, info in files.items()]], sort_keys=True).encode()).hexdigest()
                plan = InteractiveInstallPlan(plugin, installer, root,
                    tuple((path, path, False) for path in files), tuple(files.values()),
                    prepared.archive, archive_identity, signature, revision, cancel, state)
                plan.validate(prepared.game.game_id)
                log(f"Using {plugin.name}: {installer.label} (desktop installer).")
                return plan
            if (not isinstance(result, dict) or set(result) != {"files"}
                    or not isinstance(result["files"], list) or not 1 <= len(result["files"]) <= 200000):
                raise ValueError("inspect_install must return None or an object containing a nonempty files list.")
            mapping, destinations = [], set()
            for row in result["files"]:
                check_cancelled()
                if not isinstance(row, dict) or set(row) != {"source", "destination"}:
                    raise ValueError("Installer files require source and destination paths.")
                source, destination = _relative(row["source"]), _relative(row["destination"])
                if source not in files:
                    raise ValueError(f"Installer source is not in the extracted inventory: {source}")
                key = destination.casefold()
                if key in destinations:
                    raise ValueError(f"Installer destinations conflict: {destination}")
                destinations.add(key)
                mapping.append((source, destination, False))
            for destination in destinations:
                parts = destination.split("/")
                if any("/".join(parts[:index]) in destinations for index in range(1, len(parts))):
                    raise ValueError("An installer destination is both a file and a directory.")
            identities = tuple(files[source] for source, _destination, _folder in mapping)
            signature = json.dumps(_signature(plugin, installer), sort_keys=True)
            archive_identity = _identity(prepared.archive)
            revision = hashlib.sha256(json.dumps([signature, archive_identity, mapping,
                [identity[2] for identity in identities]], sort_keys=True).encode()).hexdigest()
            plan = InstallPlan(plugin, installer, root, tuple(mapping), identities, prepared.archive,
                               archive_identity, signature, revision, cancel)
            plan.validate(prepared.game.game_id)
            log(f"Using {plugin.name}: {installer.label} ({len(mapping)} files).")
            return plan
        except Exception as exc:
            from Utils.operations import OperationCancelled
            if isinstance(exc, OperationCancelled):
                raise
            raise ValueError(f"{plugin.name} installer {installer.label}: {exc}") from exc
        finally:
            for key in list(sys.modules):
                if key == name or key.startswith(name + "."):
                    sys.modules.pop(key, None)
    return None
