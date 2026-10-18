from __future__ import annotations

from Games.Custom.custom_game import RootCustomGame
from Utils.deployment import CustomRule


_STEAM_BINARIES = "Binaries/Danielle/x64/Release"
_GOG_BINARIES = "Binaries/Danielle/x64-GOG/Release"

_DEFINITION = {
    "name": "Prey (2017)",
    "game_id": "Prey_2017",
    "exe_name": f"{_STEAM_BINARIES}/Prey.exe",
    "exe_name_alts": [f"{_GOG_BINARIES}/Prey.exe"],
    "deploy_type": "root",
    "mod_data_path": "",
    "steam_id": "480490",
    "nexus_game_domain": "prey2017",
    "conflict_ignore_filenames": ["*read*me*.txt", "*.md", "*.pdf"],
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

    def _binaries_dir(self) -> str:
        game_path = self._game_path
        if game_path is not None and (game_path / _GOG_BINARIES / "Prey.exe").is_file():
            return _GOG_BINARIES
        return _STEAM_BINARIES

    @property
    def custom_routing_rules(self) -> list[CustomRule]:
        binaries = self._binaries_dir()
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
            # Most Prey mods ship a bare patch_*.pak for the base game's Precache.
            CustomRule(dest="GameSDK/Precache", extensions=[".pak"],
                       loose_only=True),
            # Luma and other ReShade add-ons ship their files flat, meant for
            # the folder that holds Prey.exe.
            CustomRule(dest=binaries, folders=["Luma"], loose_only=True),
            CustomRule(dest=binaries,
                       extensions=[".addon", ".addon64", ".dll", ".exe"],
                       loose_only=True),
        ]
