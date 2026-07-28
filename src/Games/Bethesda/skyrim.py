"""
skyrim.py
Skyrim (Legendary Edition) game handler.
"""

from pathlib import Path

from Games.base_game import WizardTool
from Games.Bethesda.fallout_3 import Fallout_3


class Skyrim(Fallout_3):

    _archive_list_needs_mod_bsas = False
    # Skyrim 1.4.26+ orders plugins by plugins.txt, not file mtimes.
    _plugin_load_order_by_mtime = False
    vanilla_plugins = ["Skyrim.esm", "Update.esm"]
    vanilla_dlc_plugins = [
        "Dawnguard.esm", "HearthFires.esm", "Dragonborn.esm",
        "HighResTexturePack01.esp", "HighResTexturePack02.esp",
        "HighResTexturePack03.esp",
    ]
    synthesis_registry_name = "Skyrim"

    @property
    def wizard_tools(self) -> list[WizardTool]:
        return self._base_wizard_tools() + [
            WizardTool(
                id="install_se_skyrim",
                label="Install Script Extender (SKSE)",
                description="Download and install SKSE into the game folder.",
                dialog_class_path="wizards.script_extender.ScriptExtenderWizard",
                extra={
                    "download_url": "https://skse.silverlock.org/beta/skse_1_07_03.7z",
                    "archive_keywords": ["skse"],
                },
            ),
            WizardTool(
                id="run_wrye_bash_skyrim",
                label="Run Wrye Bash",
                description="Download and run Wrye Bash.",
                dialog_class_path="wizards.wrye_bash.WryeBashWizard",
            ),
            *self._xedit_wizard_tools(
                build="TES5Edit", id_suffix="skyrim",
                nexus_url="https://www.nexusmods.com/skyrim/mods/25859?tab=files",
                nexus_file_id=1000372359,
            ),
            WizardTool(
                id="run_skygen_skyrim",
                label="SkyGen — Patch Generator",
                description="Scan your load order for BOS / SkyPatcher patch coverage and generate new patches.",
                dialog_class_path="wizards.skygen.SkyGenWizard",
                extra={"_full_width_overlay": True},
            ),
            WizardTool(
                id="run_plugin_audit_skyrim",
                label="Plugin Audit & Cleanup",
                description=(
                    "Scan load order for safe-to-disable plugins, then clean up orphaned "
                    "SkyGen BOS/SkyPatcher INIs for plugins that must stay enabled."
                ),
                dialog_class_path="wizards.plugin_audit.PluginAuditWizard",
                extra={"_full_width_overlay": True},
            ),
        ]

    @property
    def name(self) -> str:
        return "Skyrim"

    @property
    def game_id(self) -> str:
        return "skyrim"

    @property
    def exe_name(self) -> str:
        return "SkyrimLauncher.exe"

    @property
    def steam_id(self) -> str:
        return "72850"

    @property
    def nexus_game_domain(self) -> str:
        return "skyrim"

    # Profile Groups: disabled for now — only Stardew Valley currently opts
    # in (see base_game.py's profile_groups_supported docstring) pending
    # wider review of the virtual-merge rework. Uncomment to enable once
    # this game's deploy path has been reviewed/tested.
    # @property
    # def profile_groups_supported(self) -> bool:
    #     return True

    @property
    def loot_game_type(self) -> str:
        return "Skyrim"

    @property
    def loot_masterlist_repo(self) -> str:
        return "skyrim"

    @property
    def custom_routing_rules(self) -> list:
        from Utils.deploy import CustomRule
        return [
            CustomRule(dest="", filenames=["skse_loader.exe"], flatten=True, loose_only=True),
            CustomRule(dest="", filenames=["skse*.dll"], flatten=True, loose_only=True),
            CustomRule(dest="", folders=["Data"], flatten=True, loose_only=True),
            self._saves_routing_rule([".ess"]),
        ]

    _APPDATA_SUBPATH = Path("drive_c/users/steamuser/AppData/Local/Skyrim")
    _APPDATA_SUBPATH_GOG = Path("drive_c/users/steamuser/AppData/Local/Skyrim GOG")
    _MYGAMES_SUBPATH = Path("Skyrim")
    _MYGAMES_SUBPATH_GOG = Path("Skyrim GOG")
    _ARCHIVE_INI_FILENAME = "Skyrim.ini"
    _ARCHIVE_PREFS_INI_FILENAME = "SkyrimPrefs.ini"
    _invalidation_bsa_name = "Skyrim - Invalidation.bsa"
    _invalidation_bsa_version = 0x68

    @property
    def _script_extender_exe(self) -> str:
        return "skse_loader.exe"
