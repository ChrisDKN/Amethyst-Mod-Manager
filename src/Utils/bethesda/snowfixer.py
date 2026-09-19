"""Profile paths and SnowFixer launcher settings."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Callable

from Utils.mo2.stub import Mo2GameInfo, disable_wine_incompatible_names, write_mo2_stub
from Utils.mods.modlist import ensure_mod_preserving_position, read_modlist
from Utils.wine.paths import to_wine_path


APP_DIR = "SnowFixer"
EXE_NAME = "SnowFixer.exe"
OUTPUT_NAME = "SnowFixer_output"
GITHUB_API_URL = "https://api.github.com/repos/Cl3mus33/SnowFixer/releases/latest"
_STUB_PROFILE = "Default"


def _point_to_directory(link: Path, target: Path) -> None:
    if link.is_symlink():
        if link.resolve() == target.resolve():
            return
        link.unlink()
    elif link.exists():
        raise RuntimeError(f"SnowFixer MO2 path is occupied: {link}")
    link.symlink_to(target, target_is_directory=True)


def prepare_snowfixer(game, exe: Path, pfx: Path, profile: str,
                      log_fn: Callable[[str], None] = lambda _msg: None,
                      ui_theme: str | None = None) -> Path:
    """Point SnowFixer's MO2 reader at the active staged mods and plugin order."""
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
    if any(entry.name.casefold() == OUTPUT_NAME.casefold() and entry.enabled
           for entry in read_modlist(modlist)):
        raise RuntimeError(f"Disable {OUTPUT_NAME} in the active profile before rerunning SnowFixer")

    output = staging / OUTPUT_NAME
    if output.exists() and not output.is_dir():
        raise RuntimeError(f"SnowFixer output path is occupied: {output}")
    output.mkdir(parents=True, exist_ok=True)
    ensure_mod_preserving_position(modlist, OUTPUT_NAME, enabled=False)

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
        raise RuntimeError("Could not copy the active profile's plugins.txt into SnowFixer's MO2 instance")

    settings_file = (pfx / "drive_c" / "users" / "steamuser" / "AppData"
                     / "Roaming" / "SnowFixer" / "settings.json")
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
    log_fn(f"SnowFixer settings: {settings_file}")
    log_fn(f"SnowFixer output: {output}")
    return output
