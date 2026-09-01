"""Game handler for Crimson Desert.

The game uses version-sensitive PAZ/PAMT archives.  Generic Amethyst file
deployment is therefore intentionally disabled until the archive-aware backend
has imported the active profile and proved that recovery is available.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

from Games.base_game import BaseGame
from Utils.config_paths import get_profiles_dir
from Utils.deploy import LinkMode

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
        del mode, profile, progress_fn
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
        (log_fn or (lambda _message: None))(
            "Crimson backend is healthy and parsed "
            f"{probe['pamt_dirs']} PAMT indexes. "
            "Profile import/apply is not enabled in this prototype."
        )
        raise RuntimeError(
            "Safety stop: the prototype detected a working backend but has not yet "
            "synchronised this Amethyst profile into CDUMM. No game files were changed."
        )

    def restore(self, log_fn=None, progress_fn=None):
        del log_fn, progress_fn
        raise RuntimeError(
            "Safety stop: Crimson restore is not enabled until the backend snapshot "
            "and profile mapping have been validated. No game files were changed."
        )
