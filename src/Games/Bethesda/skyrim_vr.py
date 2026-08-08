"""
skyrim_vr.py
Skyrim VR game handler.
"""

from pathlib import Path

from Games.base_game import WizardTool
from Games.Bethesda.fallout_3 import Fallout_3


class SkyrimVR(Fallout_3):

    _archive_list_needs_mod_bsas = False
    plugins_use_star_prefix = True
    plugins_include_vanilla = False
    supports_esl_flag = True
    vanilla_plugins = [
        "Skyrim.esm", "Update.esm",
        "Dawnguard.esm", "HearthFires.esm", "Dragonborn.esm",
        "SkyrimVR.esm",
    ]
    vanilla_dlc_plugins: list[str] = []
    # Skyrim VR has no Creation Club support and ships no Skyrim.ccc
    # (libloadorder/Vortex parity: hardcoded masters + SkyrimVR.esm only).
    synthesis_registry_name = "Skyrim VR"

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
                id="install_se_skyrimvr",
                label="Install Script Extender (SKSEVR)",
                description="Download and install SKSEVR into the game folder.",
                dialog_class_path="wizards.script_extender.ScriptExtenderWizard",
                extra={
                    "download_url": "https://skse.silverlock.org/beta/sksevr_2_00_12.7z",
                    "archive_keywords": ["sksevr"],
                },
            ),
            WizardTool(
                id="run_wrye_bash_skyrimvr",
                label="Run Wrye Bash",
                description="Download and run Wrye Bash.",
                dialog_class_path="wizards.wrye_bash.WryeBashWizard",
            ),
            *self._xedit_wizard_tools(
                build="TES5VREdit", id_suffix="skyrimvr", qac=False,
                nexus_url="https://www.nexusmods.com/skyrimspecialedition/mods/164?tab=files",
                nexus_file_id=495506,
            ),
            WizardTool(
                id="run_skygen_skyrimvr",
                label="SkyGen - Patch Generator",
                description="Scan your load order for BOS / SkyPatcher patch coverage and generate new patches.",
                dialog_class_path="wizards.skygen.SkyGenWizard",
                extra={"_full_width_overlay": True},
            ),
            WizardTool(
                id="run_plugin_audit_skyrimvr",
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
        return "Skyrim VR"

    @property
    def game_id(self) -> str:
        return "skyrimvr"

    @property
    def exe_name(self) -> str:
        return "SkyrimVR.exe"

    @property
    def steam_id(self) -> str:
        return "611670"

    @property
    def nexus_game_domain(self) -> str:
        return "skyrimspecialedition"

    @property
    def loot_game_type(self) -> str:
        return "SkyrimVR"

    @property
    def loot_masterlist_repo(self) -> str:
        return "skyrimvr"

    @property
    def custom_routing_rules(self) -> list:
        from Utils.deploy import CustomRule
        return [
            CustomRule(dest="", filenames=["sksevr_loader.exe"], flatten=True, loose_only=True),
            CustomRule(dest="", filenames=["sksevr*.dll"], flatten=True, loose_only=True),
            CustomRule(dest="", folders=["Data"], flatten=True, loose_only=True),
            CustomRule(dest="", folders=["bindings"], flatten=True, loose_only=True),
            CustomRule(dest="", folders=["src"], flatten=True, loose_only=True),
            self._saves_routing_rule([".ess"]),
        ]

    _APPDATA_SUBPATH = Path("drive_c/users/steamuser/AppData/Local/Skyrim VR")
    _APPDATA_SUBPATH_GOG = None
    _MYGAMES_SUBPATH = Path("Skyrim VR")
    _MYGAMES_SUBPATH_GOG = None
    _ARCHIVE_INI_FILENAME = "Skyrim.ini"
    _ARCHIVE_PREFS_INI_FILENAME = "SkyrimPrefs.ini"
    # Runs on the SSE engine fork - same reasoning as SkyrimSE (no dummy BSA).
    _invalidation_bsa_name = None
    _invalidation_bsa_version = None

    @property
    def _script_extender_exe(self) -> str:
        return "sksevr_loader.exe"
