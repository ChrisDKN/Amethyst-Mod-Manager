from __future__ import annotations

from Games.Custom.custom_game import RootCustomGame
from Utils.deployment import CustomRule


_STEAM_EXE_DIR = "Binaries/Danielle/x64/Release"
_GOG_EXE_DIR = "Binaries/Danielle/x64-GOG/Release"
_STEAM_EXE = f"{_STEAM_EXE_DIR}/Prey.exe"
_GOG_EXE = f"{_GOG_EXE_DIR}/Prey.exe"
_SAVED_GAMES = "drive_c/users/steamuser/Saved Games/Arkane Studios/Prey"

_DEFINITION = {
    "name": "Prey (2017)",
    "game_id": "Prey_2017",
    "exe_name": _STEAM_EXE,
    "exe_name_alts": [_GOG_EXE],
    "deploy_type": "root",
    "mod_data_path": "",
    "steam_id": "480490",
    "nexus_game_domain": "prey2017",
    # ReShade.ini is the user's own ReShade/Luma config (peak brightness, keys)
    # that ReShade writes at runtime; preset mods ship one only to point at
    # their ReShadePreset.ini, which the default config already does.
    "conflict_ignore_filenames": ["*read*me*.txt", "*.md", "*.pdf", "reshade.ini"],
    # Mooncrash (Whiplash) mirrors the base game's GameSDK layout.
    "mod_required_top_level_folders": [
        "GameSDK", "Whiplash", "Engine", "Binaries", "Localization",
    ],
    "mod_required_file_types": [".pak"],
    "mod_auto_strip_until_required": True,
    "mod_install_as_is_if_no_match": True,
    "restore_before_deploy": True,
    "normalize_folder_case": True,
    "filemap_casing": "upper",
    # ReShade-based mods (Luma, ReShade presets) load as dxgi.dll from the
    # executable folder.
    "wine_dll_overrides": {"dxgi": "native,builtin"},
    "restore_whitelist": [],
    "custom_frameworks": {},
    "editable": False,
}


class Prey2017(RootCustomGame):
    def __init__(self) -> None:
        super().__init__(dict(_DEFINITION))

    @property
    def is_custom(self) -> bool:
        return False

    def _exe_dir(self) -> str:
        game_path = self.get_game_path()
        if game_path is not None and (game_path / _GOG_EXE).is_file():
            return _GOG_EXE_DIR
        return _STEAM_EXE_DIR

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
            # A bare Mooncrash pak is indistinguishable from a base-game one by
            # content, so fall back to the name; Whiplash is the DLC's folder.
            CustomRule(dest="Whiplash/GameSDK/Precache",
                       filenames=["*mooncrash*.pak", "*whiplash*.pak"],
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
