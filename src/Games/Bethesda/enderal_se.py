from pathlib import Path

from Games.base_game import WizardTool
from Games.Bethesda.bethesda_base import BethesdaBase
from Games.Bethesda.skyrim_common import SKYRIM_MOD_REQUIRED_TOP_LEVEL_FOLDERS


class EnderalSE(BethesdaBase):
    # Enderal Launcher.exe already bootstraps SKSE64 itself.
    vfs_prefers_script_extender = False

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
    def additional_nexus_domains(self) -> list[str]:
        return ["skyrimspecialedition"]

    @property
    def mod_required_top_level_folders(self) -> set[str]:
        return set(SKYRIM_MOD_REQUIRED_TOP_LEVEL_FOLDERS)

    @property
    def loot_game_type(self) -> str:
        return "SkyrimSE"

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
        from Utils.deployment import CustomRule
        return [
            CustomRule(
                rule_id='enderal_se:e772bb9074f3', dest='', filenames=['skse64_loader.exe'],
                flatten=True, loose_only=True,
            ),
            CustomRule(
                rule_id='enderal_se:47e31b43f7e0', dest='', filenames=['skse64*.dll'], flatten=True,
                loose_only=True,
            ),
            CustomRule(
                rule_id='enderal_se:42b2892ccb1f', dest='', folders=['Data'], flatten=True,
                loose_only=True,
            ),
            self._saves_routing_rule([".ess"]),
        ]

    _APPDATA_SUBPATH = Path("drive_c/users/steamuser/AppData/Local/Enderal Special Edition")
    _APPDATA_SUBPATH_GOG = Path("drive_c/users/steamuser/AppData/Local/Enderal Special Edition GOG")
    _MYGAMES_SUBPATH = Path("Enderal Special Edition")
    _MYGAMES_SUBPATH_GOG = Path("Enderal Special Edition GOG")
    _CUSTOM_INI_FILENAME = "FalloutCustom.ini"
    _ARCHIVE_INI_FILENAME = "Skyrim.ini"
    _ARCHIVE_PREFS_INI_FILENAME = "SkyrimPrefs.ini"
    _PROFILE_SAVES_INI_FILES = ("Enderal.ini", "EnderalCustom.ini",
                               "Skyrim.ini", "SkyrimCustom.ini")
    _PROFILE_SAVES_PARENT_RELATIVE = True

    def _profile_saves_ini_files(self, directory: Path) -> tuple[str, ...]:
        stems = [stem for stem in ("Enderal", "Skyrim")
                 if self._resolve_ini_path(directory, f"{stem}.ini").is_file()]
        return tuple(name for stem in (stems or ["Enderal"])
                     for name in (f"{stem}.ini", f"{stem}Custom.ini"))

    @property
    def _script_extender_exe(self) -> str:
        return "skse64_loader.exe"

    def swap_launcher(self, log_fn) -> None:
        # Enderal Launcher.exe already bootstraps SKSE64; swapping breaks it.
        log_fn("  Enderal Launcher invokes SKSE64 internally - skipping launcher swap.")

    @property
    def wizard_tools(self) -> list[WizardTool]:
        return self._base_wizard_tools() + [
            WizardTool(
                id="run_wrye_bash_enderalse",
                label="Run Wrye Bash",
                description="Download and run Wrye Bash.",
                dialog_class_path="wizards.wrye_bash.WryeBashWizard",
            ),
            self._xlodgen_wizard_tool("enderalse"),
            *self._xedit_wizard_tools(
                build="EnderalSEEdit", id_suffix="enderalse",
                nexus_url="https://www.nexusmods.com/enderalspecialedition/mods/78?tab=files",
                nexus_file_id=2793,
                discord_mode="EnderalSE",
            ),
        ]
