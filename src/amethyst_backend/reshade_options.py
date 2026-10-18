from __future__ import annotations

from pathlib import Path

from .jobs import JobError
from .tool_install import _selected_game


def options(game, profile):
    from Utils.launchers.steam import game_steam_id
    from Utils.profiles.groups import is_group
    from Utils.profiles.state import read_only_profile_state
    from Utils.wine.health import HealthStatus, check_d3dcompiler_47
    from Utils.wine.registry import normalize_pfx
    from Utils.wizards.reshade import (
        API_CHOICES, OPTIONAL_SHADER_PACKS, RESHADE_HOME_URL, SHADER_BASE_URL,
    )

    with read_only_profile_state():
        selected = _selected_game(game, profile)
        if not selected.is_configured():
            raise JobError("game_not_configured", "Configure the game before installing ReShade.")
        game_path = selected.get_game_path()
        if game_path is None:
            raise JobError("game_not_configured", "The game path is not configured.")
        subdir = selected.reshade_install_subdir(game_path)
        subdir = Path(subdir) if subdir is not None else Path(".")
        if subdir.is_absolute() or ".." in subdir.parts:
            raise JobError("invalid_reshade_destination", "The game's ReShade install folder is unsafe.")

        dll_choices = {dll for _, dll in API_CHOICES}
        default_dll = selected.reshade_dll
        if default_dll not in dll_choices:
            default_dll = API_CHOICES[0][1]
        default_arch = selected.reshade_arch
        if default_arch not in (32, 64):
            default_arch = 64

        prefix = selected.get_prefix_path()
        prefix = normalize_pfx(Path(prefix)) if prefix and Path(prefix).is_dir() else None
        initialized = bool(prefix and (prefix / "drive_c").is_dir()
                           and all((prefix / name).is_file() for name in ("user.reg", "system.reg")))
        steam_id = game_steam_id(selected)
        mod_staging = selected.get_effective_mod_staging_path()
        group = is_group(profile)
        return {
            "game": {"id": str(selected.game_id), "name": str(selected.name)},
            "profile": profile.name,
            "source_page": RESHADE_HOME_URL,
            "shader_base_url": SHADER_BASE_URL,
            "rendering_apis": [
                {"label": label, "dll": dll} for label, dll in API_CHOICES
            ],
            "default_dll": default_dll,
            "architectures": [32, 64], "default_architecture": default_arch,
            "shader_packs": [
                {"index": index, "label": label, "url": url}
                for index, (label, url, _subfolder) in enumerate(OPTIONAL_SHADER_PACKS)
            ],
            "preset_extensions": [".ini", ".txt"],
            "install_subdir": "" if subdir == Path(".") else str(subdir),
            "install_modes": [
                {"mode": "game", "path": str(game_path / subdir), "available": True},
                {"mode": "root_folder", "path": str(selected.get_effective_root_folder_path() / subdir),
                 "available": True},
                {"mode": "mod", "staging_path": str(mod_staging) if mod_staging else None,
                 "requires_mod_name": True, "available": bool(mod_staging) and not group},
            ],
            "d3dcompiler_47": {
                "prefix_path": str(prefix) if prefix else None,
                "installed": initialized and check_d3dcompiler_47(prefix).status is HealthStatus.OK,
                "installable": initialized,
                "list_method": "wine.dependencies.list.start",
                "install_method": "wine.dependencies.install.start",
                "steam_app_id": steam_id or None,
            },
        }
