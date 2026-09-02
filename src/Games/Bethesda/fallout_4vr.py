"""
fallout_4vr.py
Fallout 4 VR game handler.
"""

from pathlib import Path

from Games.base_game import WizardTool
from Games.Bethesda.fallout_3 import Fallout_3


class Fallout_4VR(Fallout_3):

    _archive_list_needs_mod_bsas = False
    plugins_use_star_prefix = True
    plugins_include_vanilla = False
    supports_esl_flag = True
    vanilla_plugins = ["Fallout4.esm", "Fallout4_VR.esm"]
    vanilla_dlc_plugins: list[str] = []
    synthesis_registry_name = "Fallout 4 VR"

    @property
    def reshade_dll(self) -> str:
        return "dxgi.dll"

    @property
    def reshade_arch(self) -> int:
        return 64

    @property
    def wizard_tools(self) -> list[WizardTool]:
        return self._base_wizard_tools() + [
            WizardTool(
                id="install_se_fo4vr",
                label="Install Script Extender (F4SEVR)",
                description="Download and install F4SEVR into the game folder.",
                dialog_class_path="wizards.script_extender.ScriptExtenderWizard",
                extra={
                    "download_url": "https://www.nexusmods.com/fallout4/mods/42159",
                    "archive_keywords": ["Fallout 4 Script Extender VR"],
                },
            ),
            WizardTool(
                id="run_wrye_bash_fo4vr",
                label="Run Wrye Bash",
                description="Download and run Wrye Bash.",
                dialog_class_path="wizards.wrye_bash.WryeBashWizard",
            ),
            *self._xedit_wizard_tools(
                build="FO4VREdit", id_suffix="fo4vr", qac=False,
                nexus_url="https://www.nexusmods.com/fallout4/mods/2737?tab=files",
                nexus_file_id=312782,
            ),
        ]

    @property
    def name(self) -> str:
        return "Fallout 4 VR"

    @property
    def game_id(self) -> str:
        return "Fallout4VR"

    @property
    def exe_name(self) -> str:
        return "Fallout4VR.exe"

    @property
    def steam_id(self) -> str:
        return "611660"

    @property
    def nexus_game_domain(self) -> str:
        return "fallout4"
    
    @property
    def custom_routing_rules(self) -> list:
        from Utils.deployment import CustomRule
        return [
            CustomRule(dest="", filenames=["f4sevr_steam_loader.dll"], flatten=True, loose_only=True),
            CustomRule(dest="", filenames=["f4sevr_loader.exe"], flatten=True, loose_only=True),
            CustomRule(dest="", folders=["Data"], flatten=True, loose_only=True),
            CustomRule(dest="", filenames=["f4sevr*.dll"], flatten=True, loose_only=True),
            self._saves_routing_rule([".fos"]),
                ]

    @property
    def loot_game_type(self) -> str:
        return "Fallout4VR"

    @property
    def archive_extensions(self) -> frozenset[str]:
        return frozenset({".ba2"})

    @property
    def loot_masterlist_repo(self) -> str:
        return "fallout4vr"

    _APPDATA_SUBPATH = Path("drive_c/users/steamuser/AppData/Local/Fallout4VR")
    _APPDATA_SUBPATH_GOG = None
    _MYGAMES_SUBPATH = Path("Fallout4VR")
    _MYGAMES_SUBPATH_GOG = None
    _ARCHIVE_INI_FILENAME = "Fallout4.ini"
    _ARCHIVE_PREFS_INI_FILENAME = "Fallout4Prefs.ini"
    _archive_invalidation_extra_keys = (("sResourceDataDirsFinal", ""),)
    # BA2-based - no dummy BSA.
    _invalidation_bsa_name = None
    _invalidation_bsa_version = None

    @property
    def _script_extender_exe(self) -> str:
        return "f4sevr_loader.exe"
