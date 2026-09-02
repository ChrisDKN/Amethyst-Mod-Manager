"""
oblivion.py
Oblivion game handler.
"""

from pathlib import Path

from Games.base_game import WizardTool
from Games.Bethesda.fallout_3 import Fallout_3


class Oblivion(Fallout_3):

    # Don't force/reorder mod BSAs in SArchiveList (inherits False). Oblivion
    # auto-loads a mod's BSA via plugin-name association (the ESP loads it),
    # AFTER the vanilla archives. Reliable BSA-over-vanilla override needs the
    # SkyBSA OBSE plugin (reverses the in-memory list so the latest-loaded BSA
    # wins); forcing the mod BSA early would invert that, and the 256-char
    # SArchiveList limit makes registration impractical anyway.
    _archive_list_needs_mod_bsas = False
    # OblivionPrefs.ini does NOT manage SArchiveList; writing a partial list
    # there shadowed Oblivion.ini's full list and broke BSA loading for every
    # mod. Keep the archive list out of the Prefs INI.
    _archive_list_in_prefs_ini = False
    vanilla_plugins = ["Oblivion.esm", "Update.esm"]
    vanilla_dlc_plugins = [
        "DLCShiveringIsles.esp", "Knights.esp",
        "DLCBattlehornCastle.esp", "DLCFrostcrag.esp",
        "DLCSpellTomes.esp", "DLCMehrunesRazor.esp",
        "DLCOrrery.esp", "DLCThievesDen.esp",
        "DLCVileLair.esp", "DLCHorseArmor.esp",
    ]
    synthesis_registry_name = "Oblivion"

    @property
    def wizard_tools(self) -> list[WizardTool]:
        return self._base_wizard_tools() + [
            WizardTool(
                id="install_se_oblivion",
                label="Install Script Extender (OBSE)",
                description="Download and install OBSE into the game folder.",
                dialog_class_path="wizards.script_extender.ScriptExtenderWizard",
                extra={
                    "download_url": "https://www.nexusmods.com/oblivion/mods/37952",
                    "archive_keywords": ["xobse"],
                },
            ),
            WizardTool(
                id="run_wrye_bash_oblivion",
                label="Run Wrye Bash",
                description="Download and run Wrye Bash.",
                dialog_class_path="wizards.wrye_bash.WryeBashWizard",
            ),
            *self._xedit_wizard_tools(
                build="TES4Edit", id_suffix="oblivion",
                nexus_url="https://www.nexusmods.com/oblivion/mods/11536?tab=files",
                nexus_file_id=1000038294,
            ),
        ]

    @property
    def name(self) -> str:
        return "Oblivion"

    @property
    def game_id(self) -> str:
        return "Oblivion"

    @property
    def exe_name(self) -> str:
        return "OblivionLauncher.exe"

    @property
    def plugin_extensions(self) -> list[str]:
        return [".esp", ".esm"]

    @property
    def steam_id(self) -> str:
        return "22330"

    @property
    def nexus_game_domain(self) -> str:
        return "oblivion"

    @property
    def loot_game_type(self) -> str:
        return "Oblivion"

    @property
    def loot_masterlist_repo(self) -> str:
        return "oblivion"

    @property
    def custom_routing_rules(self) -> list:
        from Utils.deployment import CustomRule
        return [
            CustomRule(dest="", filenames=["obse_loader.exe"], flatten=True, loose_only=True),
            CustomRule(dest="", folders=["Data"], flatten=True, loose_only=True),
            CustomRule(dest="", filenames=["obse*.dll"], flatten=True, loose_only=True),
            self._saves_routing_rule([".ess"]),
        ]

    _APPDATA_SUBPATH = Path("drive_c/users/steamuser/AppData/Local/Oblivion")
    _APPDATA_SUBPATH_GOG = Path("drive_c/users/steamuser/AppData/Local/Oblivion GOG")
    _MYGAMES_SUBPATH = Path("Oblivion")
    _MYGAMES_SUBPATH_GOG = Path("Oblivion GOG")
    _PLUGINS_TXT_FILENAME = "Plugins.txt"
    _ARCHIVE_INI_FILENAME = "Oblivion.ini"
    _ARCHIVE_PREFS_INI_FILENAME = "OblivionPrefs.ini"
    # MO2-style dummy-BSA invalidation (Oblivion engine: bsa version 0x67).
    _invalidation_bsa_name = "Oblivion - Invalidation.bsa"
    _invalidation_bsa_version = 0x67

    @property
    def _script_extender_exe(self) -> str:
        return "obse_loader.exe"

    def _delete_dummy_bsa_file(self, _log) -> None:
        """Also clean up any legacy ArchiveInvalidation.txt left from the
        pre-migration codepath."""
        super()._delete_dummy_bsa_file(_log)
        if self._game_path is None:
            return
        legacy = self._game_path / "ArchiveInvalidation.txt"
        if legacy.is_file():
            try:
                legacy.unlink()
                _log("  Removed legacy ArchiveInvalidation.txt.")
            except OSError:
                pass
