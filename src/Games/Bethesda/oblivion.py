"""
oblivion.py
Oblivion game handler.
"""

from pathlib import Path

from Games.base_game import WizardTool
from Games.Bethesda.fallout_3 import Fallout_3
from Utils.deployment import LinkMode


class Oblivion(Fallout_3):

    post_deploy_failure_is_fatal = True
    vfs_physical_game_mutation_note = (
        "BSA timestamps may be adjusted until Restore; game contents are unchanged"
    )

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
                id="oblivion_4gb_patch",
                label="Apply 4GB Patch",
                description=(
                    "Patch Oblivion.exe to use up to 4 GB of memory "
                    "(keeps a backup that can be restored)."
                ),
                dialog_class_path="wizards.oblivion_4gb_patch.Oblivion4GbPatchWizard",
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
            CustomRule(rule_id='oblivion:05fe14b74ba2', dest="", filenames=["obse_loader.exe"], flatten=True, loose_only=True),
            CustomRule(rule_id='oblivion:42b2892ccb1f', dest="", folders=["Data"], flatten=True, loose_only=True),
            CustomRule(rule_id='oblivion:5ecd4e8458ef', dest="", filenames=["obse*.dll"], flatten=True, loose_only=True),
            self._saves_routing_rule([".ess"]),
        ]

    @property
    def restore_whitelist(self) -> list:
        from Utils.bethesda.oblivion4gb import BACKUP_NAME
        from Utils.deployment import RestoreWhitelistRule
        return super().restore_whitelist + [
            RestoreWhitelistRule(path="", filenames=[BACKUP_NAME]),
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

    @property
    def _archive_timestamp_backup(self) -> Path:
        return self.get_effective_filemap_path().parent / "oblivion_bsa_timestamps.json"

    def deploy(self, log_fn=None, mode: LinkMode = LinkMode.HARDLINK,
               profile: str = "default", progress_fn=None) -> None:
        from Utils.bethesda.oblivion_archives import restore_archive_timestamps
        # Restore shared inode timestamps before an incremental VFS rebuild drops links.
        restore_archive_timestamps(
            self._archive_timestamp_backup, log_fn or (lambda _: None))
        super().deploy(log_fn=log_fn, mode=mode, profile=profile,
                       progress_fn=progress_fn)

    def post_deploy(self, log_fn=None) -> None:
        super().post_deploy(log_fn=log_fn)
        if not self.archive_invalidation or self._game_path is None:
            return
        from Utils.bethesda.oblivion_archives import backdate_archives
        data_dir = self._game_path / "Data"
        if self.vfs_launch_enabled:
            from Utils.vfs import virtual_data_write_path
            data_dir = virtual_data_write_path(self, ".")
        backdate_archives(
            data_dir, self._archive_timestamp_backup, log_fn or (lambda _: None))

    def revert_archive_invalidation(self, log_fn) -> None:
        from Utils.bethesda.oblivion_archives import restore_archive_timestamps
        restore_archive_timestamps(self._archive_timestamp_backup, log_fn)
        super().revert_archive_invalidation(log_fn)

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
