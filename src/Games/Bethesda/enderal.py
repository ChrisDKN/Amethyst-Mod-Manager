"""
enderal.py
Enderal game handler.
"""

from pathlib import Path

from Games.base_game import WizardTool
from Games.Bethesda.fallout_3 import Fallout_3


class Enderal(Fallout_3):

    _archive_list_needs_mod_bsas = False
    # Skyrim LE engine - plugins.txt-ordered, not file mtimes.
    _plugin_load_order_by_mtime = False
    vanilla_plugins = ["Skyrim.esm", "Update.esm", "Enderal - Forgotten Stories.esm"]
    vanilla_dlc_plugins = [
        "Dawnguard.esm", "HearthFires.esm", "Dragonborn.esm",
        "HighResTexturePack01.esp", "HighResTexturePack02.esp",
        "HighResTexturePack03.esp",
    ]
    synthesis_registry_name = "Enderal"

    @property
    def name(self) -> str:
        return "Enderal"

    @property
    def game_id(self) -> str:
        return "enderal"

    @property
    def exe_name(self) -> str:
        return "Enderal Launcher.exe"

    @property
    def steam_id(self) -> str:
        return "933480"

    @property
    def nexus_game_domain(self) -> str:
        return "enderal"

    @property
    def additional_nexus_domains(self) -> list[str]:
        return ["skyrim"]

    @property
    def loot_game_type(self) -> str:
        return "Skyrim"

    @property
    def loot_masterlist_repo(self) -> str:
        return "enderal"

    @property
    def custom_routing_rules(self) -> list:
        from Utils.deploy import CustomRule
        return [
            CustomRule(dest="", filenames=["skse_loader.exe"], flatten=True, loose_only=True),
            CustomRule(dest="", filenames=["skse*.dll"], flatten=True, loose_only=True),
            CustomRule(dest="", folders=["Data"], flatten=True, loose_only=True),
            self._saves_routing_rule([".ess"]),
        ]

    _APPDATA_SUBPATH = Path("drive_c/users/steamuser/AppData/Local/enderal")
    _APPDATA_SUBPATH_GOG = Path("drive_c/users/steamuser/AppData/Local/enderal GOG")
    _MYGAMES_SUBPATH = Path("Enderal")
    _MYGAMES_SUBPATH_GOG = Path("Enderal GOG")
    _ARCHIVE_INI_FILENAME = "Enderal.ini"
    _ARCHIVE_PREFS_INI_FILENAME = "EnderalPrefs.ini"
    _invalidation_bsa_name = "Enderal - Invalidation.bsa"
    _invalidation_bsa_version = 0x68

    @property
    def _script_extender_exe(self) -> str:
        return "skse_loader.exe"

    def swap_launcher(self, log_fn) -> None:
        # Enderal Launcher.exe already bootstraps SKSE; swapping breaks it.
        log_fn("  Enderal Launcher invokes SKSE internally - skipping launcher swap.")

    def _restore_launcher(self, log_fn) -> None:
        # Migration path: undo any prior swap left over from earlier versions.
        super()._restore_launcher(log_fn)

    @property
    def wizard_tools(self) -> list[WizardTool]:
        return self._base_wizard_tools() + [
            WizardTool(
                id="run_wrye_bash_enderal",
                label="Run Wrye Bash",
                description="Download and run Wrye Bash.",
                dialog_class_path="wizards.wrye_bash.WryeBashWizard",
            ),
            *self._xedit_wizard_tools(
                build="EnderalEdit", id_suffix="enderal",
                nexus_url="https://www.nexusmods.com/enderal/mods/23?tab=files",
                nexus_file_id=1331,
                discord_mode="Enderal",
            ),
        ]
