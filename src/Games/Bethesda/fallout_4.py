"""
fallout_4.py
Fallout 4 game handler.
"""

from pathlib import Path

from Games.base_game import WizardTool
from Games.Bethesda.fallout_3 import Fallout_3


class Fallout_4(Fallout_3):

    _archive_list_needs_mod_bsas = False
    plugins_use_star_prefix = True
    plugins_include_vanilla = False
    supports_esl_flag = True
    vanilla_plugins = [
        "Fallout4.esm",
        "DLCRobot.esm", "DLCworkshop01.esm", "DLCCoast.esm",
        "DLCworkshop02.esm", "DLCworkshop03.esm", "DLCNukaWorld.esm",
        "DLCUltraHighResolution.esm",
    ]
    vanilla_dlc_plugins: list[str] = []
    vanilla_ccc_filename = "Fallout4.ccc"
    synthesis_registry_name = "Fallout4"

    @property
    def reshade_dll(self) -> str:
        return "dxgi.dll"

    @property
    def reshade_arch(self) -> int:
        return 64

    @property
    def wizard_tools(self) -> list[WizardTool]:
        from Utils.wizard_gates import find_mod_exe
        bodyslide_tools = []
        if find_mod_exe(self, ("BodySlide.exe", "BodySlide x64.exe")) is not None:
            bodyslide_tools.append(WizardTool(
                id="run_bodyslide_fo4",
                label="Run BodySlide",
                description="Deploy mods and run BodySlide from the Data folder.",
                dialog_class_path="wizards.bodyslide.BodySlideWizard",
            ))
        if find_mod_exe(self, ("OutfitStudio.exe", "OutfitStudio x64.exe")) is not None:
            bodyslide_tools.append(WizardTool(
                id="run_outfitstudio_fo4",
                label="Run Outfit Studio",
                description="Deploy mods and run Outfit Studio from the Data folder.",
                dialog_class_path="wizards.bodyslide.OutfitStudioWizard",
            ))
        # Native Linux builds — always listed: the wizard downloads the
        # AppImage itself, so there is no staged exe to gate on.
        bodyslide_tools.append(WizardTool(
            id="run_bodyslide_linux_fo4",
            label="Run BodySlide (Linux)",
            description="Download and run the native Linux BodySlide, no Proton prefix needed.",
            dialog_class_path="wizards.bodyslide_linux.BodySlideLinuxWizard",
        ))
        bodyslide_tools.append(WizardTool(
            id="run_outfitstudio_linux_fo4",
            label="Run Outfit Studio (Linux)",
            description="Download and run the native Linux Outfit Studio, no Proton prefix needed.",
            dialog_class_path="wizards.bodyslide_linux.OutfitStudioLinuxWizard",
        ))
        return self._base_wizard_tools() + bodyslide_tools + [
            WizardTool(
                id="install_se_fo4",
                label="Install Script Extender (F4SE)",
                description="Download and install F4SE into the game folder.",
                dialog_class_path="wizards.script_extender.ScriptExtenderWizard",
                extra={
                    "download_url": "https://www.nexusmods.com/fallout4/mods/42147",
                    "archive_keywords": ["Fallout 4 Script Extender"],
                },
            ),
            WizardTool(
                id="run_bethini_fo4",
                label="Run BethINI Pie",
                description="Install BethINI Pie and configure Fallout 4 INI settings.",
                dialog_class_path="wizards.bethini.BethINIWizard",
            ),
            WizardTool(
                id="run_wrye_bash_fo4",
                label="Run Wrye Bash",
                description="Download and run Wrye Bash.",
                dialog_class_path="wizards.wrye_bash.WryeBashWizard",
            ),
            *self._xedit_wizard_tools(
                build="FO4Edit", id_suffix="fo4",
                nexus_url="https://www.nexusmods.com/fallout4/mods/2737?tab=files",
                nexus_file_id=312782,
            ),
        ]

    @property
    def name(self) -> str:
        return "Fallout 4"

    @property
    def game_id(self) -> str:
        return "Fallout4"

    @property
    def exe_name(self) -> str:
        return "Fallout4Launcher.exe"

    @property
    def steam_id(self) -> str:
        return "377160"

    @property
    def nexus_game_domain(self) -> str:
        return "fallout4"

    @property
    def loot_game_type(self) -> str:
        return "Fallout4"

    @property
    def archive_extensions(self) -> frozenset[str]:
        return frozenset({".ba2"})

    @property
    def custom_routing_rules(self) -> list:
        from Utils.deploy import CustomRule
        return [
            CustomRule(dest="", filenames=["f4se_loader.exe"], flatten=True, loose_only=True),
            CustomRule(dest="", filenames=["f4se*.dll"], flatten=True, loose_only=True),
            CustomRule(dest="", folders=["Data"], flatten=True, loose_only=True),
            CustomRule(dest="", filenames=["CustomControlMap.txt"], flatten=True, loose_only=True),
            self._saves_routing_rule([".fos"]),
                ]

    @property
    def loot_masterlist_repo(self) -> str:
        return "fallout4"

    _APPDATA_SUBPATH = Path("drive_c/users/steamuser/AppData/Local/Fallout4")
    _APPDATA_SUBPATH_GOG = Path("drive_c/users/steamuser/AppData/Local/Fallout4 GOG")
    _MYGAMES_SUBPATH = Path("Fallout4")
    _MYGAMES_SUBPATH_GOG = Path("Fallout4 GOG")
    _ARCHIVE_INI_FILENAME = "Fallout4.ini"
    _ARCHIVE_PREFS_INI_FILENAME = "Fallout4Prefs.ini"
    _archive_invalidation_extra_keys = (("sResourceDataDirsFinal", ""),)
    # Disable the AE launcher's Creations/Bethesda.net platform sync, which
    # otherwise rewrites plugins.txt on launch and clobbers our load order.
    _ini_override_keys = (("Bethesda.net", "bEnablePlatform", "0"),)
    # The AE launcher rewrites plugins.txt on launch — mark it read-only.
    _lock_plugins_txt = True
    # BA2-based — no dummy BSA, only the bInvalidateOlderFiles INI key.
    _invalidation_bsa_name = None
    _invalidation_bsa_version = None

    @property
    def _script_extender_exe(self) -> str:
        return "f4se_loader.exe"
