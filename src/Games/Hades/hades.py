from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

from Games.Custom.custom_game import RootCustomGame
from Utils.deployment import LinkMode


_DEFINITION = {
    "name": "Hades",
    "game_id": "Hades",
    "exe_name": "x64/Hades.exe",
    "exe_name_alts": ["x64Vk/Hades.exe", "x86/Hades.exe"],
    "deploy_type": "root",
    "mod_data_path": "",
    "steam_id": "1145360",
    "nexus_game_domain": "hades",
    "auto_install_deps": ["vcredist"],
    "conflict_ignore_filenames": ["license", "*read*.txt", "*.md"],
    "mod_folder_strip_prefixes_post": ["Content", "Mods"],
    "mod_required_top_level_folders": ["Content", "x64", "x86", "x64VK"],
    "mod_auto_strip_until_required": True,
    "mod_install_as_is_if_no_match": True,
    "restore_before_deploy": True,
    "normalize_folder_case": True,
    "filemap_casing": "upper",
    "custom_routing_rules": [
        {
            "dest": "Content",
            "folders": ["Mods"],
            "flatten": True,
        },
        {
            "dest": "Content/Mods",
            "filenames": ["modfile.txt"],
            "include_siblings": True,
        },
        {
            "dest": "Content",
            "filenames": ["modimporter", "modimporter.py"],
            "flatten": True,
        },
        {
            "dest": "Content",
            "folders": ["sjson"],
            "flatten": True,
        },
        {
            "dest": "",
            "folders": ["Content"],
            "flatten": True,
        },
        {
            "dest": "Content",
            "folders": [
                "Win", "Subtitles", "Scripts", "Movies", "Maps", "Game", "Audio",
            ],
            "flatten": True,
        },
        {
            "dest": "Content/Scripts",
            "extensions": [".lua"],
            "loose_only": True,
        },
        {
            "dest": "Content/Movies",
            "folders": ["720p"],
            "flatten": True,
        },
        {
            "dest": "Content/Movies",
            "extensions": [".bik"],
            "flatten": True,
        },
        {
            "dest": "Content/Game/Weapons",
            "filenames": [
                "Weapons.sjson", "PlayerWeapons.sjson", "EnemyWeapons.sjson",
            ],
            "loose_only": True,
        },
    ],
    "restore_whitelist": [],
    "custom_frameworks": {},
    "editable": False,
}

_IMPORTER_MODULE = None


def _importer_module():
    global _IMPORTER_MODULE
    if _IMPORTER_MODULE is not None:
        return _IMPORTER_MODULE
    path = Path(__file__).resolve().parent / "hades_mod_importer.py"
    name = "Games._hades_mod_importer"
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load the built-in Hades Mod Importer: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    _IMPORTER_MODULE = module
    return module


class Hades(RootCustomGame):
    def __init__(self) -> None:
        super().__init__(dict(_DEFINITION))

    @property
    def is_custom(self) -> bool:
        return False

    @property
    def mod_staging_requires_subdir(self) -> bool:
        return True

    @property
    def mod_staging_wrap_signals(self) -> tuple[set[str], set[str]]:
        return ({"modfile.txt"}, set())

    @property
    def mod_staging_already_structured_markers(self) -> set[str]:
        return {"modfile.txt"}

    def deploy(self, log_fn=None, mode: LinkMode = LinkMode.HARDLINK,
               profile: str = "default", progress_fn=None) -> None:
        super().deploy(log_fn=log_fn, mode=mode, profile=profile,
                       progress_fn=progress_fn)
        if not self.vfs_launch_enabled:
            _importer_module().apply_mod_imports(self._game_path, log_fn=log_fn)

    def _vfs_post_view_build(self, *, view_root: Path, **_kwargs) -> None:
        _importer_module().apply_mod_imports(
            view_root, log_fn=_kwargs.get("log_fn"))

    def restore(self, log_fn=None, progress_fn=None) -> None:
        if self._game_path is not None:
            _importer_module().restore_mod_imports(
                self._game_path, log_fn=log_fn)
        super().restore(log_fn=log_fn, progress_fn=progress_fn)

    def post_clean_game_folder(self, log_fn=None) -> None:
        if self._game_path is not None:
            _importer_module().restore_mod_imports(
                self._game_path, log_fn=log_fn)
