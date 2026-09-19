"""Shared profile setup for MO2-aware Bethesda patchers."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Callable

from Utils.mo2.stub import Mo2GameInfo, disable_wine_incompatible_names, write_mo2_stub
from Utils.mods.modlist import ensure_mod_preserving_position, read_modlist
from Utils.wine.paths import to_wine_path


_STUB_PROFILE = "Default"


def _point_to_directory(link: Path, target: Path) -> None:
    if link.is_symlink():
        if link.resolve() == target.resolve():
            return
        link.unlink()
    elif link.exists():
        raise RuntimeError(f"MO2 path is occupied: {link}")
    link.symlink_to(target, target_is_directory=True)


def _copy_skyrim_ini(game, pfx: Path, profile_dir: Path,
                     log_fn: Callable[[str], None]) -> None:
    docs = game._MYGAMES_DOCS
    subdir = game._MYGAMES_SUBPATH
    game_pfx = game.get_prefix_path()
    game_ini = (Path(game_pfx) / docs / subdir / "Skyrim.ini"
                if game_pfx is not None else None)
    if game_pfx is not None and Path(game_pfx).resolve() == pfx.resolve():
        if game_ini is None or not game_ini.is_file():
            raise RuntimeError("Skyrim.ini was not found in the game prefix")
        return
    profile_ini = profile_dir / "ini files" / "Skyrim.ini"
    source = next((path for path in (game_ini, profile_ini)
                   if path is not None and path.is_file()), None)
    if source is None:
        raise RuntimeError("Skyrim.ini was not found in the game prefix or active profile")

    target = pfx / docs / subdir / "Skyrim.ini"
    if target.is_file() and target.resolve() == source.resolve():
        return
    existing_parent = target.parent
    while not existing_parent.exists() and not existing_parent.is_symlink():
        existing_parent = existing_parent.parent
    if (not existing_parent.resolve().is_relative_to(pfx.resolve())
            or target.is_symlink()):
        raise RuntimeError(f"Cannot safely copy Skyrim.ini into {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.is_file() and target.read_bytes() == source.read_bytes():
        return
    shutil.copy2(source, target)
    log_fn(f"Copied Skyrim.ini into tool prefix: {target}")


def prepare_mo2_patcher(game, exe: Path, pfx: Path, profile: str, *,
                        tool_name: str, output_name: str, settings_file: Path,
                        log_fn: Callable[[str], None] = lambda _msg: None,
                        ui_theme: str | None = None) -> Path:
    """Point a patcher at the active staged mods and plugin order."""
    profile_dir = game.get_profile_root() / "profiles" / profile
    game.set_active_profile_dir(profile_dir)
    game.load_paths()
    profile_dir = game.get_profile_root() / "profiles" / profile

    game_path = game.get_game_path()
    if game_path is None or not (game_path / "Data").is_dir():
        raise RuntimeError("Skyrim's game folder or Data folder is not configured")
    staging = game.get_effective_mod_staging_path()
    modlist = profile_dir / "modlist.txt"
    plugins = profile_dir / "plugins.txt"
    if not staging.is_dir() or not modlist.is_file() or not plugins.is_file():
        raise RuntimeError("The active profile needs its mods, modlist.txt and plugins.txt")
    if any(entry.name.casefold() == output_name.casefold() and entry.enabled
           for entry in read_modlist(modlist)):
        raise RuntimeError(f"Disable {output_name} in the active profile before rerunning {tool_name}")

    _copy_skyrim_ini(game, pfx, profile_dir, log_fn)

    output = staging / output_name
    if output.exists() and not output.is_dir():
        raise RuntimeError(f"{tool_name} output path is occupied: {output}")
    output.mkdir(parents=True, exist_ok=True)
    ensure_mod_preserving_position(modlist, output_name, enabled=False)

    overwrite = game.get_effective_overwrite_path()
    overwrite.mkdir(parents=True, exist_ok=True)
    instance = exe.parent / "amm_mo2_dummy"
    instance.mkdir(parents=True, exist_ok=True)
    direct_root = staging.parent if staging == game.get_profile_root() / "mods" else None
    if direct_root is None:
        _point_to_directory(instance / "mods", staging)
        _point_to_directory(instance / "overwrite", overwrite)
    write_mo2_stub(
        instance,
        prefix=pfx,
        mod_directory=staging,
        profile_name=_STUB_PROFILE,
        modlist_src=modlist,
        overwrite_dir=overwrite,
        plugins_txt=plugins,
        game_info=Mo2GameInfo(
            "Skyrim Special Edition" if game.game_id == "skyrim_se" else "Skyrim",
            "Steam", game_path),
        modlist_transforms=[disable_wine_incompatible_names()],
        log_fn=log_fn,
    )
    if direct_root is not None:
        ini = instance / "ModOrganizer.ini"
        ini.write_text(
            ini.read_text(encoding="utf-8").replace(
                f"base_directory={to_wine_path(instance, pfx)}",
                f"base_directory={to_wine_path(direct_root, pfx)}"),
            encoding="utf-8",
        )
    elif (instance / "profiles" / _STUB_PROFILE / "plugins.txt").read_bytes() != plugins.read_bytes():
        raise RuntimeError(f"Could not copy the active profile's plugins.txt into {tool_name}'s MO2 instance")

    settings = {}
    if settings_file.is_file():
        try:
            settings = json.loads(settings_file.read_text(encoding="utf-8"))
            if not isinstance(settings, dict):
                settings = {}
        except (OSError, ValueError):
            settings = {}
    settings.update({
        "GameLocation": to_wine_path(game_path, pfx),
        "GameType": 0 if game.game_id == "skyrim_se" else 1,
        "OutputLocation": to_wine_path(output, pfx),
        "ModManager": 1,
        "Mo2InstancePath": to_wine_path(instance, pfx),
        "Mo2ProfileName": profile if direct_root is not None else _STUB_PROFILE,
    })
    if ui_theme in ("dark", "light"):
        settings["UiTheme"] = ui_theme
    settings_file.parent.mkdir(parents=True, exist_ok=True)
    settings_file.write_text(json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8")
    log_fn(f"{tool_name} settings: {settings_file}")
    log_fn(f"{tool_name} output: {output}")
    return output
