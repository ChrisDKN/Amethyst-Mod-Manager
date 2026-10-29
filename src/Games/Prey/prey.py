from __future__ import annotations

import gzip
import re
import shutil
import xml.etree.ElementTree as ET
import zipfile
from functools import cache
from pathlib import Path

from Games.Custom.custom_game import RootCustomGame
from Utils.deployment import CustomRule


_STEAM_EXE_DIR = "Binaries/Danielle/x64/Release"
# Other stores ship Prey.exe in a sibling folder; paths from the Vortex Prey
# extension (GOG also confirmed by Lutris' installer).
_OTHER_STORE_EXE_DIRS = ("Binaries/Danielle/x64-GOG/Release", "Binaries/Danielle/x64-Epic/Release")
_STEAM_EXE = f"{_STEAM_EXE_DIR}/Prey.exe"
_SAVED_GAMES = "drive_c/users/steamuser/Saved Games/Arkane Studios/Prey"
# Same character set ChairManager's ModInfo::ValidateModName accepts.
_CHAIRLOADER_MOD_NAME = re.compile(r"[A-Za-z0-9._-]+")
_MOONCRASH_PRECACHE = "Whiplash/GameSDK/Precache"

_DEFINITION = {
    "name": "Prey (2017)",
    "game_id": "Prey_2017",
    "exe_name": _STEAM_EXE,
    "exe_name_alts": [f"{exe_dir}/Prey.exe" for exe_dir in _OTHER_STORE_EXE_DIRS],
    "deploy_type": "root",
    "mod_data_path": "",
    "steam_id": "480490",
    "nexus_game_domain": "prey2017",
    # ReShade.ini is the user's own ReShade/Luma config (peak brightness, keys)
    # that ReShade writes at runtime; preset mods ship one only to point at
    # their ReShadePreset.ini, which the default config already does.
    "conflict_ignore_filenames": ["*read*me*.txt", "*.md", "*.pdf", "reshade.ini"],
    # Mooncrash (Whiplash) mirrors the base game's GameSDK layout. Data/ is the
    # asset tree of a Chairloader mod and stops auto-strip from flattening it;
    # ChairloaderPatch/ marks the Chairloader framework itself, whose sample
    # mods would otherwise be stripped up to the top and taken for a mod.
    "mod_required_top_level_folders": [
        "GameSDK", "Whiplash", "Engine", "Binaries", "Localization", "Data",
        "ChairloaderPatch", "Levels",
    ],
    "mod_required_file_types": [".pak"],
    "mod_auto_strip_until_required": True,
    "mod_install_as_is_if_no_match": True,
    "restore_before_deploy": True,
    "normalize_folder_case": True,
    "filemap_casing": "upper",
    "wine_dll_overrides": {"dxgi": "native,builtin", "mswsock": "native,builtin"},
    "restore_whitelist": [],
    "custom_frameworks": {},
    "editable": False,
}


def _move_staged_files_into(dest_root: Path, folder: Path,
                           source_root: Path | None = None) -> None:
    """Move everything a mod staged into *folder*, inside the same mod."""
    target = dest_root / folder
    target.mkdir(parents=True, exist_ok=True)
    top = dest_root / folder.parts[0]
    for entry in list((source_root or dest_root).iterdir()):
        # meta.ini is Amethyst's own record, written before install hooks run.
        if entry.name.lower() == "meta.ini" or entry == top:
            continue
        shutil.move(str(entry), str(target / entry.name))


def _stage_chairloader_framework_in_own_folder(dest_root: Path, mod_name: str, log_fn) -> None:
    """Keep the Chairloader release together in Chairloader/: ChairManager runs
    from its own folder, and left loose its .exe/.dll would be routed next to Prey.exe."""
    if not any(p.name.lower() == "chairmanager.exe" for p in dest_root.iterdir()):
        return
    _move_staged_files_into(dest_root, Path("Chairloader"))
    log_fn(f"{mod_name}: Chairloader framework staged as Chairloader/; run ChairManager.exe from there.")


def _stage_chairloader_mod_under_mods(dest_root: Path, mod_name: str, log_fn) -> None:
    """Move a staged Chairloader mod into Mods/<modName>/, the only place
    ChairManager looks for it; it rejects a folder not named after ModInfo.xml."""
    source_root = dest_root
    protected = {name.lower() for name in _DEFINITION["mod_required_top_level_folders"]}
    protected.update({"mods", "chairloader"})
    while True:
        entries = list(source_root.iterdir())
        mod_info = next((p for p in entries
                         if p.is_file() and p.name.lower() == "modinfo.xml"), None)
        if mod_info is not None:
            break
        folders = [p for p in entries if p.is_dir() and not p.is_symlink()]
        if len(folders) != 1 or folders[0].name.lower() in protected:
            return
        source_root = folders[0]
    try:
        chairloader_name = ET.parse(mod_info).getroot().get("modName", "")
    except ET.ParseError as exc:
        log_fn(f"{mod_name}: unreadable ModInfo.xml ({exc}); left in place.")
        return
    if chairloader_name in {".", ".."} or not _CHAIRLOADER_MOD_NAME.fullmatch(chairloader_name):
        log_fn(f"{mod_name}: ModInfo.xml has no valid modName; left in place.")
        return
    _move_staged_files_into(dest_root, Path("Mods") / chairloader_name, source_root)
    while source_root != dest_root:
        try:
            source_root.rmdir()
        except OSError:
            break
        source_root = source_root.parent
    log_fn(f"{mod_name}: Chairloader mod staged as Mods/{chairloader_name}; "
           "enable it in ChairManager.")


@cache
def _game_only_files(game: str) -> frozenset[str]:
    # GameSDK-relative paths (lowercase) present in only this game's vanilla
    # paks, listed from the pak directories of Steam build 5297220.
    listing = Path(__file__).with_name(f"{game}_only_files.txt.gz")
    return frozenset(gzip.decompress(listing.read_bytes()).decode().splitlines())


def _move_mooncrash_paks_to_whiplash(dest_root: Path, mod_name: str, log_fn) -> None:
    """Send a loose pak to Mooncrash's Precache when it overrides files only
    Mooncrash has and none only the base game has; any other pak keeps the
    normal routing rules."""
    for pak in [p for p in dest_root.iterdir() if p.is_file() and p.suffix.lower() == ".pak"]:
        try:
            with zipfile.ZipFile(pak) as archive:
                inner = {name.replace("\\", "/").lower() for name in archive.namelist()}
        except zipfile.BadZipFile:
            continue
        touches_mooncrash = not inner.isdisjoint(_game_only_files("mooncrash"))
        touches_base = not inner.isdisjoint(_game_only_files("base"))
        if touches_mooncrash and not touches_base:
            target = dest_root / _MOONCRASH_PRECACHE
            target.mkdir(parents=True, exist_ok=True)
            shutil.move(str(pak), str(target / pak.name))
            log_fn(f"{mod_name}: {pak.name} overrides Mooncrash-only files; staged for Mooncrash.")


class Prey2017(RootCustomGame):
    def __init__(self) -> None:
        super().__init__(dict(_DEFINITION))

    @property
    def is_custom(self) -> bool:
        return False

    def _exe_dir(self) -> str:
        game_path = self.get_game_path()
        if game_path is not None:
            for exe_dir in _OTHER_STORE_EXE_DIRS:
                if (game_path / exe_dir / "Prey.exe").is_file():
                    return exe_dir
        return _STEAM_EXE_DIR

    @property
    def additional_install_logic(self) -> list:
        return [_stage_chairloader_framework_in_own_folder, _stage_chairloader_mod_under_mods,
                _move_mooncrash_paks_to_whiplash]

    @property
    def excluded_loose_filenames(self) -> set[str]:
        # Changelogs, instructions, screenshots and debug symbols authors ship
        # beside the mod; nothing Prey loads sits loose in a mod's top level.
        return {"*.txt", "*.png", "*.jpg", "*.jpeg", "*.pdb", "license", "*.license"}

    @property
    def custom_routing_rules(self) -> list[CustomRule]:
        exe_dir = self._exe_dir()
        return [
            # Loose replacements of vanilla files go where the original lives;
            # these must win over the generic Precache rule below.
            CustomRule(dest="Engine", filenames=[
                "engine.pak", "shaders.pak", "shadersbin_d3d11.pak",
                "shadercache_d3d11.pak", "shadercachestartup_d3d11.pak",
            ], loose_only=True),
            CustomRule(dest="Localization", filenames=[
                "*_xml.pak", "*_xml_patch.pak", "*_anim.pak", "*_anim_patch.pak",
            ], loose_only=True),
            CustomRule(dest="GameSDK", filenames=["game.cfg"], loose_only=True),
            CustomRule(dest="GameSDK", folders=["Levels"], loose_only=True),
            # A bare Mooncrash pak usually overrides the same files as its base-game
            # twin, so fall back to the name; Whiplash is the DLC's folder and
            # authors who ship both variants suffix the DLC one with _mc.
            CustomRule(dest=_MOONCRASH_PRECACHE,
                       filenames=["*mooncrash*.pak", "*whiplash*.pak", "*_mc.pak"],
                       loose_only=True),
            # Most Prey mods ship a bare patch_*.pak for the base game's Precache.
            CustomRule(dest="GameSDK/Precache", extensions=[".pak"],
                       loose_only=True),
            # ReShade add-ons (Luma) and DLL mods (Prey Interface Customizer's
            # dinput8.dll) ship flat files meant for the folder that holds Prey.exe.
            CustomRule(dest=exe_dir, folders=["Luma"], loose_only=True),
            CustomRule(dest=exe_dir,
                       extensions=[".addon", ".addon64", ".dll", ".exe"],
                       loose_only=True),
            # ReShade presets: shaders and preset .ini files sit next to ReShade's dxgi.dll.
            CustomRule(dest=exe_dir, folders=["reshade-shaders"], loose_only=True),
            CustomRule(dest=exe_dir, extensions=[".ini"], loose_only=True),
            # Compiled shader caches (Shuttle Bay Fix) go to the user's Saved Games folder.
            CustomRule(dest=_SAVED_GAMES, folders=["Shaders"], loose_only=True,
                       to_prefix=True),
        ]
