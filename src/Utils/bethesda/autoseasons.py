"""AutoSeasons installation and active-profile configuration."""

from __future__ import annotations

import json
import shutil
import tempfile
import zipfile
from pathlib import Path, PureWindowsPath
from typing import Callable

from Utils.mods.modlist import ensure_mod_preserving_position, read_modlist
from Utils.wine.paths import to_wine_path


EXE_NAME = "AutoSeasons.exe"
MOD_NAME = "AutoSeasons"
OUTPUT_NAME = "AutoSeasons_output"
GITHUB_API_URL = "https://api.github.com/repos/Cl3mus33/AutoSeasons/releases/latest"


def find_autoseasons_exe(game) -> Path | None:
    from Utils.wizards.gates import find_staged_exe

    indexed = find_staged_exe(game, EXE_NAME)
    if indexed is not None:
        return indexed
    staging = game.get_effective_mod_staging_path()
    if staging is None or not staging.is_dir():
        return None
    for mod in staging.iterdir():
        if mod.is_dir():
            for candidate in (mod / EXE_NAME, mod / MOD_NAME / EXE_NAME):
                if candidate.is_file():
                    return candidate
    return None


def install_autoseasons(game, archive: Path, profile: str,
                        log_fn: Callable[[str], None] = lambda _msg: None) -> Path:
    from Utils.mods.install_as_mod import index_installed_mod, register_as_mod_neutral

    profile_dir = game.get_profile_root() / "profiles" / profile
    game.set_active_profile_dir(profile_dir)
    game.load_paths()
    staging = game.get_effective_mod_staging_path()
    if staging is None or not staging.is_dir():
        raise RuntimeError("Mod staging folder is not configured")
    dest = staging / MOD_NAME
    if dest.exists() or dest.is_symlink():
        raise RuntimeError(f"{MOD_NAME} already exists in the mod staging folder")

    with tempfile.TemporaryDirectory(prefix="autoseasons-", dir=staging) as temp:
        extracted = Path(temp)
        with zipfile.ZipFile(archive) as bundle:
            for info in bundle.infolist():
                relative = PureWindowsPath(info.filename.replace("/", "\\"))
                if (relative.is_absolute() or relative.drive
                        or any(part in ("", ".", "..") for part in relative.parts)):
                    raise RuntimeError(f"Unsafe path in AutoSeasons archive: {info.filename}")
                target = extracted.joinpath(*relative.parts)
                if info.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with bundle.open(info) as source, target.open("wb") as output:
                        shutil.copyfileobj(source, output)
        if not (extracted / EXE_NAME).is_file():
            raise RuntimeError(f"{EXE_NAME} was not found in the downloaded archive")
        extracted.rename(dest)

    register_as_mod_neutral(
        game, MOD_NAME, archive, modlist_path=profile_dir / "modlist.txt",
        log_fn=log_fn, root_folder=False)
    index_installed_mod(game, MOD_NAME, log_fn=log_fn)
    log_fn(f"Installed AutoSeasons as a regular mod: {dest}")
    return dest / EXE_NAME


def prepare_autoseasons(game, exe: Path, pfx: Path, profile: str,
                        log_fn: Callable[[str], None] = lambda _msg: None,
                        ui_theme: str | None = None) -> Path:
    from Utils.bethesda.mo2_patcher import _copy_skyrim_ini

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
    if staging is None or not staging.is_dir() or not modlist.is_file() or not plugins.is_file():
        raise RuntimeError("The active profile needs its mods, modlist.txt and plugins.txt")
    if any(entry.name.casefold() == OUTPUT_NAME.casefold() and entry.enabled
           for entry in read_modlist(modlist)):
        raise RuntimeError(f"Disable {OUTPUT_NAME} and deploy again before rerunning AutoSeasons")

    output = staging / OUTPUT_NAME
    if output.is_symlink() or (output.exists() and not output.is_dir()):
        raise RuntimeError(f"AutoSeasons output path is occupied: {output}")
    output.mkdir(parents=True, exist_ok=True)
    ensure_mod_preserving_position(modlist, OUTPUT_NAME, enabled=False)

    game_pfx = game.get_prefix_path()
    gog_source = (Path(game_pfx) / game._APPDATA_SUBPATH_GOG / "plugins.txt"
                  if game_pfx is not None else None)
    steam_source = (Path(game_pfx) / game._APPDATA_SUBPATH / "plugins.txt"
                    if game_pfx is not None else None)
    is_gog = bool(gog_source is not None and gog_source.is_file()
                  and (steam_source is None or not steam_source.is_file()))
    if is_gog:
        (pfx / game._APPDATA_SUBPATH_GOG).mkdir(parents=True, exist_ok=True)
    game._symlink_plugins_txt(profile, log_fn, prefix_root=pfx)
    targets = game._plugins_txt_targets(pfx)
    if not any(target.is_file() and target.read_bytes() == plugins.read_bytes()
               for target in targets):
        raise RuntimeError("Could not copy the active plugins.txt into AutoSeasons' prefix")
    _copy_skyrim_ini(
        game, pfx, profile_dir, log_fn,
        subdir=game._MYGAMES_SUBPATH_GOG if is_gog else None)

    config_file = exe.parent / "AutoSeasons_config.json"
    config = {}
    if config_file.is_file():
        try:
            config = json.loads(config_file.read_text(encoding="utf-8"))
            if not isinstance(config, dict):
                config = {}
        except (OSError, ValueError):
            config = {}
    config.update({
        "gameDir": to_wine_path(game_path, pfx),
        "gameType": "skyrimgog" if is_gog else "skyrimse",
        "outputDir": to_wine_path(output, pfx),
    })
    if ui_theme in ("dark", "light"):
        config["uiTheme"] = ui_theme
    config_file.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
    log_fn(f"AutoSeasons config: {config_file}")
    log_fn(f"AutoSeasons output: {output}")
    return output
