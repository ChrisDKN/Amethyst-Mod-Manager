from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

NEXUS_URL = "https://www.nexusmods.com/skyrimspecialedition/mods/187138?tab=files"
NEXUS_MOD_ID = 187138
EXE_NAME = "EasyNPC.exe"
APP_DIR = "EasyNPC Next"


def update_app_settings(prefix: Path, *, data: Path, mods: Path,
                        exe: Path, game_release: str, log_fn) -> None:
    from Utils.atomic_write import write_atomic_text
    from Utils.wine.paths import to_wine_path

    settings_path = (prefix / "drive_c/users/steamuser/AppData/Local/EasyNPC"
                     / "Settings.json")
    if settings_path.is_file():
        settings = json.loads(settings_path.read_text(encoding="utf-8-sig"))
        if not isinstance(settings, dict):
            raise RuntimeError(f"Invalid EasyNPC settings: {settings_path}")
    else:
        settings = {}
    previous = dict(settings)
    settings.update({
        "gameDataDirectory": to_wine_path(data, prefix),
        "gameRelease": game_release,
        "modRootDirectory": to_wine_path(mods, prefix),
        "useModManagerForModDirectory": False,
    })
    if not settings.get("mugshotsDirectory"):
        settings["mugshotsDirectory"] = to_wine_path(
            exe.parent / "Mugshots", prefix)
    if settings != previous:
        write_atomic_text(settings_path, json.dumps(settings, ensure_ascii=False,
                                                  indent=2) + "\n")
        log_fn(f"EasyNPC settings: filled game Data, {game_release}, and active mod root.")


def set_wine_compat_version(proton_script: Path, env: dict, *, log_fn) -> None:
    from Utils.launchers.steam import proton_run_command

    cmd = proton_run_command(
        proton_script, "runinprefix", "reg", "add",
        rf"HKCU\Software\Wine\AppDefaults\{EXE_NAME}",
        "/v", "Version", "/t", "REG_SZ", "/d", "win7", "/f",
        env=env,
    )
    result = subprocess.run(cmd, env=env, capture_output=True, text=True,
                            timeout=60)
    if result.returncode:
        detail = (result.stderr or result.stdout or "").strip()[:300]
        raise RuntimeError(f"Could not set EasyNPC Windows 7 compatibility: {detail}")
    log_fn("EasyNPC.exe: Wine Windows 7 compatibility set for this executable.")


def find_exe(game) -> Path | None:
    from Utils.bethesda.xedit import tool_exe_path
    return tool_exe_path(game, EXE_NAME, APP_DIR)


def latest_main_file(api):
    files = api.get_mod_files("skyrimspecialedition", NEXUS_MOD_ID).files
    main = [file for file in files if file.category_id == 1
            or file.category_name.upper() == "MAIN"]
    if not main:
        raise RuntimeError("Nexus Mods returned no EasyNPC Next Main file.")
    return max(main, key=lambda file: (file.uploaded_timestamp, file.file_id))


def find_archive(*, since: float = 0) -> Path | None:
    from Utils.downloads.locations import get_effective_download_locations
    from Utils.wizards.archives import is_archive

    found = []
    for folder in get_effective_download_locations():
        try:
            entries = folder.iterdir()
            for path in entries:
                name = path.name.casefold()
                if (not is_archive(name) or "verify" in name
                        or "post-build" in name or "post_build" in name):
                    continue
                if "easynpc next" not in name and "easynpc-next" not in name:
                    if not re.search(r"(?<!\d)187138(?!\d)", name):
                        continue
                if path.is_file():
                    stat = path.stat()
                    if stat.st_size > 0 and stat.st_mtime >= since:
                        found.append((stat.st_mtime, path))
        except OSError:
            continue
    return max(found, default=(0, None), key=lambda item: item[0])[1]


def launch_args(game, exe: Path, prefix: Path, profile: str, *, log_fn):
    from Utils.mo2.stub import (
        Mo2GameInfo, disable_wine_incompatible_names, write_mo2_stub,
    )
    from Utils.vfs import effective_tool_data_root
    from Utils.wine.paths import to_wine_path

    profile_dir = game.get_profile_root() / "profiles" / profile
    staging = game.get_effective_mod_staging_path()
    game_path = game.get_game_path()
    if game_path is None or not (game_path / "Data").is_dir():
        raise RuntimeError("Skyrim's game and Data folders are required.")
    if not staging.is_dir() or not (profile_dir / "plugins.txt").is_file():
        raise RuntimeError("The active profile needs staged mods and plugins.txt.")

    instance = exe.parent / "amm_mo2_dummy"
    write_mo2_stub(
        instance, prefix=prefix, mod_directory=staging,
        profile_name="Default", modlist_src=profile_dir / "modlist.txt",
        overwrite_dir=game.get_effective_overwrite_path(),
        plugins_txt=profile_dir / "plugins.txt",
        game_info=Mo2GameInfo(
            "Skyrim VR" if game.game_id == "skyrimvr" else "Skyrim Special Edition",
            "Steam", game_path),
        modlist_transforms=[disable_wine_incompatible_names()],
        log_fn=log_fn,
    )
    loadorder = profile_dir / "loadorder.txt"
    if loadorder.is_file():
        shutil.copyfile(loadorder, instance / "profiles" / "Default" / "loadorder.txt")
    mo2_exe = instance / "ModOrganizer.exe"
    mo2_exe.touch(exist_ok=True)
    data = effective_tool_data_root(game)
    game_release = "SkyrimVR" if game.game_id == "skyrimvr" else "SkyrimSE"
    update_app_settings(prefix, data=data, mods=staging, exe=exe,
                        game_release=game_release, log_fn=log_fn)
    return [
        "--game", game_release,
        "--game-path", to_wine_path(data, prefix),
        "--mo2-exe", to_wine_path(mo2_exe, prefix),
    ]
