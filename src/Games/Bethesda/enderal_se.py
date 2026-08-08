"""
enderal_se.py
Enderal Special Edition game handler.
"""

from pathlib import Path

from Games.base_game import WizardTool
from Games.Bethesda.fallout_3 import Fallout_3


class EnderalSE(Fallout_3):

    _archive_list_needs_mod_bsas = False
    plugins_use_star_prefix = True
    plugins_include_vanilla = False
    supports_esl_flag = True
    vanilla_plugins = [
        "Skyrim.esm", "Update.esm",
        "Dawnguard.esm", "HearthFires.esm", "Dragonborn.esm",
        "Enderal - Forgotten Stories.esm",
    ]
    vanilla_dlc_plugins: list[str] = []
    synthesis_registry_name = "Enderal Special Edition"

    @property
    def name(self) -> str:
        return "Enderal SE"

    @property
    def game_id(self) -> str:
        return "enderalse"

    @property
    def exe_name(self) -> str:
        return "Enderal Launcher.exe"

    @property
    def steam_id(self) -> str:
        return "976620"

    @property
    def nexus_game_domain(self) -> str:
        return "enderalspecialedition"

    @property
    def loot_game_type(self) -> str:
        return "SkyrimSE"

    # SSE engine is 64-bit - without these the Fallout_3 branch's d3d9/32-bit
    # ReShade config is inherited and the wizard installs a 32-bit d3d9.dll.
    @property
    def reshade_dll(self) -> str:
        return "dxgi.dll"

    @property
    def reshade_arch(self) -> int:
        return 64

    @property
    def loot_masterlist_repo(self) -> str:
        return "enderal"

    @property
    def custom_routing_rules(self) -> list:
        from Utils.deploy import CustomRule
        return [
            CustomRule(dest="", filenames=["skse64_loader.exe"], flatten=True, loose_only=True),
            CustomRule(dest="", filenames=["skse64*.dll"], flatten=True, loose_only=True),
            CustomRule(dest="", folders=["Data"], flatten=True, loose_only=True),
            self._saves_routing_rule([".ess"]),
        ]

    _APPDATA_SUBPATH = Path("drive_c/users/steamuser/AppData/Local/Enderal Special Edition")
    _APPDATA_SUBPATH_GOG = Path("drive_c/users/steamuser/AppData/Local/Enderal Special Edition GOG")
    _MYGAMES_SUBPATH = Path("Enderal Special Edition")
    _MYGAMES_SUBPATH_GOG = Path("Enderal Special Edition GOG")
    _ARCHIVE_INI_FILENAME = "Skyrim.ini"
    _ARCHIVE_PREFS_INI_FILENAME = "SkyrimPrefs.ini"
    # SSE-engine: see SkyrimSE - no dummy-BSA needed.
    _invalidation_bsa_name = None
    _invalidation_bsa_version = None

    @property
    def _script_extender_exe(self) -> str:
        return "skse64_loader.exe"

    def swap_launcher(self, log_fn) -> None:
        # Enderal Launcher.exe already bootstraps SKSE64; swapping breaks it.
        log_fn("  Enderal Launcher invokes SKSE64 internally - skipping launcher swap.")

    def _restore_launcher(self, log_fn) -> None:
        # Migration path: undo any prior swap left over from earlier versions.
        super()._restore_launcher(log_fn)

    @property
    def wizard_tools(self) -> list[WizardTool]:
        return self._base_wizard_tools() + [
            WizardTool(
                id="run_wrye_bash_enderalse",
                label="Run Wrye Bash",
                description="Download and run Wrye Bash.",
                dialog_class_path="wizards.wrye_bash.WryeBashWizard",
            ),
            *self._xedit_wizard_tools(
                build="EnderalSEEdit", id_suffix="enderalse",
                nexus_url="https://www.nexusmods.com/enderalspecialedition/mods/78?tab=files",
                nexus_file_id=2793,
                discord_mode="EnderalSE",
            ),
        ]
