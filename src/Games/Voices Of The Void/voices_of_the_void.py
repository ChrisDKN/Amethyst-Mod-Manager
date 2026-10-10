from Games.Custom.custom_game import Ue5CustomGame
from Games.base_game import WizardTool


_DEFINITION = {
    "name": "Voices Of The Void",
    "game_id": "Voices_Of_The_Void",
    "exe_name": "WindowsNoEditor/VotV/Binaries/Win64/VotV-Win64-Shipping.exe",
    "exe_name_alts": ["WindowsNoEditor/VotV.exe"],
    "deploy_type": "ue5",
    "mod_data_path": "WindowsNoEditor/VotV",
    "steam_id": "",
    "nexus_game_domain": "",
    "thunderstore_community": "voices-of-the-void",
    "auto_install_deps": ["vcredist"],
    "mod_folder_strip_prefixes": [
        "GAMEID", "Content", "Paks", "~mods", "Binaries", "Win64", "ue4ss",
    ],
    "conflict_ignore_filenames": ["LICENSE", "*.md", "icon.png", "manifest.json"],
    "restore_before_deploy": True,
    "normalize_folder_case": True,
    "filemap_casing": "upper",
    "wine_dll_overrides": {"dwmapi": "native,builtin"},
    "custom_routing_rules": [
        {"dest": "Content/Paks", "folders": ["LogicMods"], "flatten": True},
        {
            "dest": "Content/Paks/~mods",
            "extensions": [".pak"],
            "companion_extensions": [".ucas", ".utoc"],
            "include_siblings": True,
        },
        {
            "dest": "Binaries/Win64",
            "extensions": [".asi"],
            "companion_extensions": [".ini"],
        },
        {"dest": "Binaries/Win64", "filenames": ["ue4ss-settings.ini"], "flatten": True},
        {"dest": "Binaries/Win64", "folders": ["Mods"], "flatten": True},
        {"dest": "Binaries/Win64/Mods", "folders": ["scripts"], "include_siblings": True},
        {"dest": "Binaries/Win64/Mods", "filenames": ["enabled.txt"], "include_siblings": True},
        {"dest": "Binaries/Win64/Mods", "folders": ["dlls"], "include_siblings": True},
        {"dest": "Binaries/Win64/Mods", "filenames": ["mods.txt"], "flatten": True},
    ],
    "custom_frameworks": {"UE4SS": "Binaries/Win64/dwmapi.dll"},
    "editable": False,
}


class VoicesOfTheVoid(Ue5CustomGame):
    def __init__(self) -> None:
        super().__init__(dict(_DEFINITION))

    @property
    def is_custom(self) -> bool:
        return False

    @property
    def wizard_tools(self) -> list[WizardTool]:
        return self._base_wizard_tools() + [WizardTool(
            id="update_yeetpatch",
            label="Update Game (YeetPatch)",
            description="Download YeetPatch and update Voices of the Void in a terminal.",
            dialog_class_path="wizards_qt.yeetpatch_view.YeetPatchView",
            category="Setup and Installers",
        )]

    def _resolve_ue4ss_mods_dest(self) -> None:
        return None

    def _resolve_filemap_entries(
        self, entries: list[tuple[str, str]],
    ) -> list[tuple[str, str, str, str]]:
        resolved = super()._resolve_filemap_entries(entries)
        routed = []
        destinations = {
            "pak": "Content/Paks/LogicMods",
            "cfg": "Config",
            "mod": "Binaries/Win64/Mods",
        }
        for staged_rel, mod_name, dest, final in resolved:
            head, sep, tail = staged_rel.replace("\\", "/").partition("/")
            target = destinations.get(head.casefold()) if sep else None
            if target and dest != self._PREFIX_SKIP_DEST:
                if head.casefold() == "mod":
                    if mod_name in {"", ".", ".."} or "/" in mod_name or "\\" in mod_name:
                        raise ValueError(f"Invalid mod folder name: {mod_name!r}")
                    tail = f"{mod_name}/{tail}"
                routed.append((staged_rel, mod_name, target, tail))
            else:
                routed.append((staged_rel, mod_name, dest, final))
        return self._canonicalize_routed_dir_casing(routed)
