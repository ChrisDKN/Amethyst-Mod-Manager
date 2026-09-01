"""Game handler for Crimson Desert.

The game uses version-sensitive PAZ/PAMT archives.  Generic Amethyst file
deployment is therefore intentionally disabled until the archive-aware backend
has imported the active profile and proved that recovery is available.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from pathlib import Path

from Games.base_game import BaseGame
from Utils.atomic_write import write_atomic_text
from Utils.config_paths import get_profiles_dir
from Utils.deploy import LinkMode
from Utils.modlist import read_modlist

_PROFILES_DIR = get_profiles_dir()


def _load_backend_module():
    sibling = Path(__file__).resolve().parent / "crimson_desert_backend.py"
    spec = importlib.util.spec_from_file_location("crimson_desert_backend", sibling)
    if spec is None or spec.loader is None:
        raise RuntimeError("Could not load the Crimson Desert backend adapter.")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class CrimsonDesert(BaseGame):
    deploy_mode_supports_copy = True
    deploy_mode_fallback = LinkMode.COPY
    profile_groups_supported = False

    def __init__(self):
        self._game_path: Path | None = None
        self._prefix_path: Path | None = None
        self._staging_path: Path | None = None
        self._deploy_mode = LinkMode.COPY
        self.load_paths()

    @property
    def name(self) -> str:
        return "Crimson Desert"

    @property
    def game_id(self) -> str:
        return "crimson_desert"

    @property
    def exe_name(self) -> str:
        return "bin64/CrimsonDesert.exe"

    @property
    def steam_id(self) -> str:
        return "3321460"

    @property
    def nexus_game_domain(self) -> str:
        return "crimsondesert"

    @property
    def plugin_extensions(self) -> list[str]:
        return []

    @property
    def loot_sort_enabled(self) -> bool:
        return False

    def get_game_path(self) -> Path | None:
        return self._game_path

    def get_mod_data_path(self) -> Path | None:
        # CDUMM owns its overlay below the game root.  Returning CDMods keeps
        # Amethyst's open-location UI useful without exposing vanilla archives
        # as a generic deployment destination.
        return self._game_path / "CDMods" if self._game_path else None

    def get_mod_staging_path(self) -> Path:
        if self._staging_path is not None:
            return self._staging_path / "mods"
        return _PROFILES_DIR / self.name / "mods"

    def get_prefix_path(self) -> Path | None:
        return self._prefix_path

    def get_deploy_mode(self) -> LinkMode:
        return self._deploy_mode

    def set_staging_path(self, path: Path | str | None) -> None:
        self._staging_path = Path(path) if path else None
        self.save_paths()

    def set_prefix_path(self, path: Path | str | None) -> None:
        self._prefix_path = Path(path) if path else None
        self.save_paths()

    def set_deploy_mode(self, mode: LinkMode) -> None:
        self._deploy_mode = mode
        self.save_paths()

    def deploy(self, log_fn=None, mode=LinkMode.COPY, profile="default", progress_fn=None):
        del mode, progress_fn
        log = log_fn or (lambda _message: None)
        backend = _load_backend_module()
        command = backend.discover_backend()
        if command is None:
            raise RuntimeError(
                "Crimson Desert needs the archive-aware CDUMM backend. "
                "Set AMETHYST_CDUMM_COMMAND or AMETHYST_CDUMM_ROOT."
            )
        result = backend.self_check(command)
        if not result.get("ok"):
            raise RuntimeError(f"Crimson backend self-check failed: {result.get('errors', {})}")
        if self._game_path is None:
            raise RuntimeError("Crimson Desert game path is not configured.")
        probe = backend.probe_game(command, self._game_path)
        log(f"Crimson backend parsed {probe['pamt_dirs']} PAMT indexes.")

        profile_dir = self.get_profile_root() / "profiles" / profile
        staging = self.get_mod_staging_path()
        enabled = [
            entry for entry in read_modlist(profile_dir / "modlist.txt")
            if entry.enabled and not entry.is_separator
        ]
        supported = (".zip", ".7z", ".rar", ".cdmod", ".json")
        sources: list[tuple[str, Path]] = []
        for entry in reversed(enabled):
            mod_dir = staging / entry.name
            candidates = sorted(
                path for path in mod_dir.rglob("*")
                if path.is_file() and path.name.lower() != "meta.ini"
                and (
                    path.suffix.lower() in supported
                    or path.name.lower().endswith(".field.json")
                )
            )
            if len(candidates) != 1:
                raise RuntimeError(
                    f"Crimson mod '{entry.name}' must contain exactly one supported "
                    f"CDUMM source; found {len(candidates)}."
                )
            sources.append((entry.name, candidates[0]))

        backend.ensure_snapshot(command, self._game_path, log_fn=log)
        mapping_path = self._game_path / "CDMods" / "amethyst-profile.json"
        try:
            mapping = json.loads(mapping_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            mapping = {"mods": {}}
        managed = mapping.setdefault("mods", {})

        for name, source in sources:
            hasher = hashlib.sha256()
            with source.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    hasher.update(chunk)
            digest = hasher.hexdigest()
            record = managed.get(name, {})
            if record.get("sha256") != digest:
                if record.get("mod_id"):
                    result = backend.import_mod(
                        command, self._game_path, source,
                        existing_mod_id=int(record["mod_id"]), log_fn=log,
                    )
                else:
                    result = backend.import_mod(
                        command, self._game_path, source, log_fn=log,
                    )
                if result.get("error"):
                    raise RuntimeError(str(result["error"]))
                record = {"mod_id": int(result["mod_id"]), "sha256": digest}
                managed[name] = record

        enabled_names = {name for name, _source in sources}
        for name, record in managed.items():
            backend.set_enabled(
                command, self._game_path, int(record["mod_id"]), name in enabled_names
            )
        write_atomic_text(mapping_path, json.dumps(mapping, indent=2) + "\n")
        backend.apply(command, self._game_path, log_fn=log)
        active = [mod["name"] for mod in backend.list_mods(command, self._game_path)
                  if mod.get("status") == "active"]
        log(f"Crimson deploy complete; active backend mods: {', '.join(active) or 'none'}.")

    def restore(self, log_fn=None, progress_fn=None):
        del progress_fn
        backend = _load_backend_module()
        command = backend.discover_backend()
        if command is None or self._game_path is None:
            raise RuntimeError("Crimson Desert backend or game path is unavailable.")
        backend.revert(command, self._game_path, log_fn=log_fn)
