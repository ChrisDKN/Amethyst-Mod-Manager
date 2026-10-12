"""Fallout 3 game handler."""

from pathlib import Path

from Games.base_game import WizardTool
from Games.Bethesda.bethesda_base import BethesdaBase


class Fallout_3(BethesdaBase):
    _extra_auto_install_deps = ("lavfilters",)
    _launcher_alts = ["FalloutLauncher.exe"]
    vanilla_plugins = ["Fallout3.esm"]
    vanilla_dlc_plugins = [
        "Anchorage.esm", "ThePitt.esm", "BrokenSteel.esm",
        "PointLookout.esm", "Zeta.esm",
    ]
    synthesis_registry_name = "Fallout3"

    @property
    def name(self) -> str:
        return "Fallout 3"

    @property
    def game_id(self) -> str:
        return "Fallout3"

    @property
    def exe_name(self) -> str:
        return "Fallout3Launcher.exe"

    @property
    def steam_id(self) -> str:
        return "22300"

    @property
    def nexus_game_domain(self) -> str:
        return "fallout3"

    @property
    def loot_game_type(self) -> str:
        return "Fallout3"

    @property
    def loot_masterlist_repo(self) -> str:
        return "fallout3"

    @property
    def custom_routing_rules(self) -> list:
        from Utils.deployment import CustomRule
        return [
            CustomRule(
                rule_id='fallout_3:abb8b8d9e72f', dest='', filenames=['fose_loader.exe'],
                flatten=True, loose_only=True,
            ),
            CustomRule(
                rule_id='fallout_3:42b2892ccb1f', dest='', folders=['Data'], flatten=True,
                loose_only=True,
            ),
            CustomRule(
                rule_id='fallout_3:853eca2b27b4', dest='', filenames=['fose*.dll'], flatten=True,
                loose_only=True,
            ),
            self._saves_routing_rule([".fos"]),
        ]

    @staticmethod
    def _fo3_mpi_wizard_tools(id_suffix: str) -> list[WizardTool]:
        return [
            WizardTool(
                id=f"{id_suffix}_bsa_decompressor",
                label="BSA Decompressor",
                description=(
                    "Decompress the vanilla BSA archives for faster loading "
                    "(native Linux MPI installer) and add the result as a mod. "
                    "Needs the FO3 BSA Decompressor download from Nexus."
                ),
                dialog_class_path="wizards.bsa_decompressor.BSADecompressorWizard",
                category="Setup and Installers",
            ),
            WizardTool(
                id=f"{id_suffix}_esm_fixes",
                label="Install Unofficial Fallout 3 ESM Patcher",
                description=(
                    "Patch the vanilla .esm masters with community bugfixes "
                    "(native Linux MPI installer) and add the result as a mod. "
                    "Needs the Unofficial Fallout 3 ESM Patcher download from "
                    "Nexus."
                ),
                dialog_class_path="wizards.esm_fixes.ESMFixesWizard",
                category="Setup and Installers",
            ),
        ]

    @property
    def wizard_tools(self) -> list[WizardTool]:
        return self._base_wizard_tools() + [
            WizardTool(
                id="downgrade_fo3",
                label="Downgrade Fallout 3",
                description=(
                    "Downgrade to pre-Anniversary Edition so that "
                    "the script extender (FOSE) works correctly."
                ),
                dialog_class_path="wizards.fallout_downgrade.FalloutDowngradeWizard",
            ),
            WizardTool(
                id="install_se_fo3",
                label="Install Script Extender (FOSE)",
                description="Download and install FOSE into the game folder.",
                dialog_class_path="wizards.script_extender.ScriptExtenderWizard",
                extra={
                    "download_url": "https://fose.silverlock.org/beta/fose_v1_3_beta2.7z",
                    "archive_keywords": ["fose"],
                },
            ),
            *self._fo3_mpi_wizard_tools("fo3"),
            WizardTool(
                id="run_wrye_bash_fo3",
                label="Run Wrye Bash",
                description="Download and run Wrye Bash.",
                dialog_class_path="wizards.wrye_bash.WryeBashWizard",
            ),
            self._xlodgen_wizard_tool("fo3"),
            *self._xedit_wizard_tools(
                build="FO3Edit", id_suffix="fo3",
                nexus_url="https://www.nexusmods.com/fallout3/mods/637?tab=files",
                nexus_file_id=1000026408,
            ),
        ]

    _APPDATA_SUBPATH = Path("drive_c/users/steamuser/AppData/Local/Fallout3")
    _APPDATA_SUBPATH_GOG: "Path | None" = Path("drive_c/users/steamuser/AppData/Local/Fallout3 GOG")
    _MYGAMES_SUBPATH = Path("Fallout3")
    _MYGAMES_SUBPATH_GOG: "Path | None" = Path("Fallout3 GOG")
    _ARCHIVE_INI_FILENAME = "FALLOUT.INI"
    _ARCHIVE_PREFS_INI_FILENAME: "str | None" = "FalloutPrefs.ini"
    _PROFILE_SAVES_INI_FILES = ("FALLOUT.INI", "FalloutCustom.ini")
    _invalidation_bsa_name: "str | None" = "Fallout - Invalidation.bsa"
    _invalidation_bsa_version: "int | None" = 0x68
    _archive_list_needs_mod_bsas: bool = True
    _archive_list_fix_name: "str | None" = "Command Extender"
    _archive_list_fix_path: "str | None" = "Data/FOSE/Plugins/CommandExtender.dll"
    _CUSTOM_INI_FILENAME = "FalloutCustom.ini"

    @property
    def _script_extender_exe(self) -> str:
        return "fose_loader.exe"
