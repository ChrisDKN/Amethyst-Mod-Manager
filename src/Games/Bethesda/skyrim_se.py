from pathlib import Path

from Games.Bethesda.bethesda_base import BethesdaBase
from Games.Bethesda.skyrim_common import SKYRIM_MOD_REQUIRED_TOP_LEVEL_FOLDERS
from Games.base_game import WizardTool, MODERN_DIRECTX_DEPS


class SkyrimSE(BethesdaBase):
    _ini_override_keys = (("Bethesda.net", "bEnablePlatform", "0"),)

    # Bind at the game root to keep deep asset paths below Windows path limits.
    vfs_bind_launch_at_game_root = True

    def get_vfs_launch_bind_root(self) -> Path | None:
        game_root = self.get_vfs_game_root()
        global_root = self.get_global_game_path()
        if (game_root is not None and global_root is not None
                and global_root.is_dir()
                and len(str(global_root)) < len(str(game_root))):
            return global_root
        return game_root

    plugins_use_star_prefix = True
    plugins_include_vanilla = False
    # The engine force-loads vanilla and Creation Club plugins outside plugins.txt.
    supports_esl_flag = True
    vanilla_plugins = [
        "Skyrim.esm", "Update.esm",
        "Dawnguard.esm", "HearthFires.esm", "Dragonborn.esm",
    ]
    vanilla_dlc_plugins: list[str] = []
    vanilla_ccc_filename = "Skyrim.ccc"
    primary_plugin_order = [
        "Skyrim.esm", "Update.esm",
        "Dawnguard.esm", "HearthFires.esm", "Dragonborn.esm",
        "ccBGSSSE001-Fish.esm",
        "ccQDRSSE001-SurvivalMode.esl",
        "ccBGSSSE037-Curios.esl",
        "ccBGSSSE025-AdvDSGS.esm",
        "_ResourcePack.esl",
    ]
    synthesis_registry_name = "Skyrim Special Edition"

    @property
    def name(self) -> str:
        return "Skyrim Special Edition"

    @property
    def game_id(self) -> str:
        return "skyrim_se"

    @property
    def exe_name(self) -> str:
        return "SkyrimSELauncher.exe"

    @property
    def steam_id(self) -> str:
        return "489830"

    @property
    def direct_launch_exes(self) -> list[str]:
        return ["SkyrimSE.exe"]

    @property
    def nexus_game_domain(self) -> str:
        return "skyrimspecialedition"

    @property
    def mod_required_top_level_folders(self) -> set[str]:
        return set(SKYRIM_MOD_REQUIRED_TOP_LEVEL_FOLDERS)

    @property
    def mod_folder_strip_prefixes_post(self) -> set[str]:
        return {"data"}

    @property
    def probe_stub_dirs(self) -> list[str]:
        return super().probe_stub_dirs + ["Data/Strings", "Data/LODSettings"]

    @property
    def loot_game_type(self) -> str:
        return "SkyrimSE"

    @property
    def loot_masterlist_repo(self) -> str:
        return "skyrimse"

    @property
    def wine_dll_overrides(self) -> dict[str, str]:
        # Allow Wine audio fallback to avoid pure-native XAudio2 crashes under Proton.
        overrides = {
            "winmm": "native,builtin",
            "version": "native,builtin",
            "d3dcompiler_47": "native",
        }
        for n in range(8):
            overrides[f"xaudio2_{n}"] = "native,builtin"
        for n in range(8):
            overrides[f"x3daudio1_{n}"] = "native,builtin"
        return overrides

    auto_install_deps = MODERN_DIRECTX_DEPS

    @property
    def reshade_dll(self) -> str:
        return "dxgi.dll"

    @property
    def reshade_arch(self) -> int:
        return 64

    @property
    def filemap_casing_pins(self) -> dict[str, str]:
        return {
            # These are mostly to fix issues with infinity UI related mods that expect exact casing otherwise they crash
            "hudmoviebaseinstance":    "HUDMovieBaseInstance",
            "compassshoutmeterholder": "CompassShoutMeterHolder",
            "infinityui": "InfinityUI",
            "hudmenu": "HUDMenu",
            "skse": "SKSE",
            "!assets": "!assets",
            "compass.swf": "Compass.swf",
            "questitemlist.swf": "QuestItemList.swf",
            "minimap.swf": "Minimap.swf",
            "minimapart.swf": "MinimapArt.swf",
            "worldmap": "WorldMap",
            "localmapmenu": "LocalMapMenu",
            "icondisplayextension.swf": "IconDisplayExtension.swf",
            "icondisplayextensionart.swf": "IconDisplayExtensionArt.swf",
            "data" : "Data",
        }

    @property
    def custom_routing_rules(self) -> list:
        from Utils.deployment import CustomRule
        return [
            CustomRule(
                rule_id='skyrim_se:37111f7bfb46', dest='', filenames=['d3dx9_42.dll'], flatten=True,
            ),
            CustomRule(
                rule_id='skyrim_se:a84e7a154053', dest='', filenames=['skse64_1*.dll'],
                flatten=True, loose_only=True,
            ),
            CustomRule(
                rule_id='skyrim_se:e772bb9074f3', dest='', filenames=['skse64_loader.exe'],
                flatten=True, loose_only=True,
            ),
            CustomRule(
                rule_id='skyrim_se:44bc033720c3', dest='', filenames=['d3dcompiler_47.dll'],
                flatten=True, loose_only=True,
            ),
            CustomRule(
                rule_id='skyrim_se:e39e7c9da11d', dest='Data/SKSE/Plugins/CharGen/Presets',
                extensions=['.jslot'], flatten=True,
            ),
            CustomRule(
                rule_id='skyrim_se:112bbe22ee8d', dest='', folders=['enbseries'], flatten=True,
            ),
            self._saves_routing_rule([".ess"]),
        ]

    @property
    def additional_install_logic(self) -> list:
        return super().additional_install_logic + [self._patch_enblocal_linux_version]

    def _patch_enblocal_linux_version(self, dest_root: Path, mod_name: str,
                                      log_fn=None) -> None:
        _log = log_fn or (lambda _m: None)
        from Games.Bethesda.bethesda_ini import _read_ini_key, _set_ini_key
        for ini_path in dest_root.rglob("*"):
            if not ini_path.is_file() or ini_path.name.lower() != "enblocal.ini":
                continue
            try:
                current = _read_ini_key(ini_path, "GLOBAL", "LinuxVersion",
                                        case_insensitive=True)
                if (current or "").strip().lower() == "true":
                    continue
                _set_ini_key(ini_path, "GLOBAL", "LinuxVersion", "true",
                             case_insensitive=True)
                _log(f"ENB: set [GLOBAL] LinuxVersion=true in "
                     f"{ini_path.relative_to(dest_root)}.")
            except OSError as exc:
                _log(f"WARN: could not patch {ini_path.name} for Linux: {exc}")

    @property
    def wizard_tools(self) -> list[WizardTool]:
        from Utils.wizards.gates import (
            engine_fixes_installed as ef_installed,
            find_mod_exe,
            sse_display_tweaks_installed as sdt_installed,
        )
        pandora_tools = []
        if sdt_installed(self):
            pandora_tools.append(WizardTool(
                id="sse_display_tweaks_skyrimse",
                label="SSE Display Tweaks Config",
                description="Create or edit SSEDisplayTweaks.ini with per-setting toggles and descriptions.",
                dialog_class_path="wizards.sse_display_tweaks.SSEDisplayTweaksWizard",
                category="INI Tweaks",
                extra={"_full_width_overlay": True},
            ))
        if ef_installed(self):
            pandora_tools.append(WizardTool(
                id="engine_fixes_skyrimse",
                label="Engine Fixes Config",
                description="Create or edit EngineFixes.toml with per-setting toggles and descriptions.",
                dialog_class_path="wizards.engine_fixes.EngineFixesWizard",
                category="INI Tweaks",
                extra={"_full_width_overlay": True},
            ))
        pandora_tools.append(WizardTool(
            id="run_pandora_skyrimse",
            label="Run Pandora",
            description="Install or run Pandora Behaviour Engine+.",
            dialog_class_path="wizards.pandora.PandoraWizard",
        ))
        if find_mod_exe(self, ("BodySlide.exe", "BodySlide x64.exe")) is not None:
            pandora_tools.append(WizardTool(
                id="run_bodyslide_skyrimse",
                label="Run BodySlide",
                description="Deploy mods and run BodySlide from the Data folder.",
                dialog_class_path="wizards.bodyslide.BodySlideWizard",
            ))
        if find_mod_exe(self, ("OutfitStudio.exe", "OutfitStudio x64.exe")) is not None:
            pandora_tools.append(WizardTool(
                id="run_outfitstudio_skyrimse",
                label="Run Outfit Studio",
                description="Deploy mods and run Outfit Studio from the Data folder.",
                dialog_class_path="wizards.bodyslide.OutfitStudioWizard",
            ))
        # Native builds are always listed; their wizards download the AppImages.
        pandora_tools.append(WizardTool(
            id="run_bodyslide_linux_skyrimse",
            label="Run BodySlide (Linux)",
            description="Download and run the native Linux BodySlide, no Proton prefix needed.",
            dialog_class_path="wizards.bodyslide_linux.BodySlideLinuxWizard",
        ))
        pandora_tools.append(WizardTool(
            id="run_outfitstudio_linux_skyrimse",
            label="Run Outfit Studio (Linux)",
            description="Download and run the native Linux Outfit Studio, no Proton prefix needed.",
            dialog_class_path="wizards.bodyslide_linux.OutfitStudioLinuxWizard",
        ))
        return self._base_wizard_tools() + pandora_tools + [
            WizardTool(
                id="downgrade_skyrimse",
                label="Downgrade Skyrim Special Edition",
                description=(
                    "Download the latest Skyrim Special Edition Steam "
                    "Downgrader (game or Creation Kit) and run it from the "
                    "game folder."),
                dialog_class_path=(
                    "wizards.skyrim_se_downgrader.SkyrimSEDowngraderWizard"),
                category="Setup and Installers",
            ),
            WizardTool(
                id="install_se_skyrimse",
                label="Install Script Extender (SKSE64)",
                description="Download and install SKSE64 into the game folder.",
                dialog_class_path="wizards.script_extender.ScriptExtenderWizard",
                extra={
                    "versions": [
                        {
                            "label": "2.3.1 Steam",
                            "description": "SKSE64 2.3.1 for Skyrim SE 1.7.104 on Steam. "
                                           "Downloads from Nexus Mods; automatic for Premium users.",
                            "download_url": "https://www.nexusmods.com/skyrimspecialedition/mods/30379?tab=files&file_id=795992",
                            "nexus_file_id": 795992,
                            "archive_keywords": ["skse64", "steam", "30379"],
                        },
                        {
                            "label": "Skyrim SE 1.6.1170 (Steam)",
                            "description": "SKSE64 2.2.6 for 1.6.1170 Steam installs.",
                            # Pin each SKSE build to its supported runtime version.
                            "github_api_url": "https://api.github.com/repos/ianpatt/skse64/releases/tags/v2.2.6",
                            "archive_keywords": ["skse64"],
                        },
                        {
                            "label": "Skyrim SE GOG 1.6.1179",
                            "description": "GOG build of SKSE64 (skse64_2_02_06_gog.7z). Not available on GitHub.",
                            "direct_download_url": "https://skse.silverlock.org/beta/skse64_2_02_06_gog.7z",
                        },
                        {
                            "label": "Skyrim SE 1.5.97 (legacy)",
                            "description": "SKSE64 2.0.20 for older 1.5.97 installs (Special Edition pre-AE).",
                            "github_api_url": "https://api.github.com/repos/ianpatt/skse64/releases/tags/v2.0.20",
                            "archive_keywords": ["skse64"],
                        },
                    ],
                },
            ),
            WizardTool(
                id="run_pgpatcher_skyrimse",
                label="Run PGPatcher",
                description="Install PGPatcher, deploy mods, and run PGPatcher.exe.",
                dialog_class_path="wizards.pgpatcher.PGPatcherWizard",
            ),
            WizardTool(
                id="run_snowfixer_skyrimse",
                label="Run SnowFixer",
                description="Install SnowFixer and generate a snow patch from the active profile.",
                dialog_class_path="wizards.snowfixer.SnowFixerWizard",
            ),
            WizardTool(
                id="run_autoblend_skyrimse",
                label="Run AutoBlend",
                description="Install AutoBlend and generate a landscape blend patch from the active profile.",
                dialog_class_path="wizards.autoblend.AutoBlendWizard",
            ),
            WizardTool(
                id="run_autoseasons_skyrimse",
                label="Run AutoSeasons",
                description="Install AutoSeasons as a mod and generate seasonal patches from the active profile.",
                dialog_class_path="wizards.autoseasons.AutoSeasonsWizard",
            ),
            WizardTool(
                id="run_cao_skyrimse",
                label="Assets Optimizer (CAO)",
                description=(
                    "Install Cathedral Assets Optimizer and optimize a selected "
                    "mod from the staging folder."),
                dialog_class_path="wizards.cao.CAOWizard",
                category="Patchers and Cleanup",
            ),
            WizardTool(
                id="run_sseedit_skyrimse",
                label="Run SSEEdit",
                description="Install SSEEdit, deploy mods, and run SSEEdit.exe.",
                dialog_class_path="wizards.sseedit.SSEEditWizard",
            ),
            WizardTool(
                id="run_sseedit_qac_skyrimse",
                label="Run SSEEdit QAC",
                description="Deploy mods and run SSEEditQuickAutoClean.exe.",
                dialog_class_path="wizards.sseedit.SSEEditQACWizard",
            ),
            WizardTool(
                id="run_xedit_discord_skyrimse",
                label="Run xEdit (Discord version)",
                description="Deploy mods and run xTESEdit.exe -SSE from the latest "
                            "xEdit build, released through the xEdit Discord.",
                dialog_class_path="wizards.sseedit.XEditDiscordWizard",
                extra={"xedit_exe": "xTESEdit.exe", "display_name": "xEdit",
                       "app_dir": "xEdit (Discord)", "discord": True,
                       "discord_mode": "SSE"},
            ),
            WizardTool(
                id="run_xedit_discord_qac_skyrimse",
                label="Run xEdit QAC (Discord version)",
                description="Deploy mods and run xTESEdit.exe -SSE -quickautoclean "
                            "from the latest xEdit build, released through the xEdit Discord.",
                dialog_class_path="wizards.sseedit.XEditDiscordQACWizard",
                extra={"xedit_exe": "xTESEdit.exe", "display_name": "xEdit",
                       "app_dir": "xEdit (Discord)", "discord": True,
                       "discord_mode": "SSE"},
            ),
            WizardTool(
                id="run_creationkit_skyrimse",
                label="Run Creation Kit",
                description="Install Creation Kit Platform Extended, deploy mods, and run CreationKit.exe.",
                dialog_class_path="wizards.creationkit.CreationKitWizard",
            ),
            WizardTool(
                id="run_eslifier_skyrimse",
                label="Run ESLifier",
                description="Install ESLifier and flag/compact plugins into the light (ESL) space.",
                dialog_class_path="wizards.eslifier.ESLifierWizard",
            ),
            WizardTool(
                id="run_texgen_skyrimse",
                label="Run TexGen",
                description="Install DynDOLOD tools, deploy mods, and run TexGenx64.exe.",
                dialog_class_path="wizards.dyndolod.TexGenWizard",
            ),
            WizardTool(
                id="run_dyndolod_skyrimse",
                label="Run DynDOLOD",
                description="Install DynDOLOD tools, deploy mods, and run DynDOLODx64.exe.",
                dialog_class_path="wizards.dyndolod.DynDOLODWizard",
            ),
            self._xlodgen_wizard_tool("skyrimse"),
            WizardTool(
                id="run_acmos_skyrimse",
                label="Run ACMOS Road Generator",
                description=(
                    "Install ACMOS Road Generator, choose a terrain LOD mod, "
                    "and write generated road textures to ACMOS_Output."),
                dialog_class_path="wizards.acmos.ACMOSWizard",
                category="DynDOLOD",
            ),
            WizardTool(
                id="run_bethini_skyrimse",
                label="Run BethINI Pie",
                description="Install BethINI Pie and configure Skyrim SE INI settings.",
                dialog_class_path="wizards.bethini.BethINIWizard",
            ),
            WizardTool(
                id="run_wrye_bash_skyrimse",
                label="Run Wrye Bash",
                description="Download and run Wrye Bash.",
                dialog_class_path="wizards.wrye_bash.WryeBashWizard",
            ),
            WizardTool(
                id="run_vramr_skyrimse",
                label="Run VRAMr",
                description="Download VRAMr from Nexus, deploy mods, and run texture optimisation.",
                dialog_class_path="wizards.vramr.VRAMrWizard",
            ),
            WizardTool(
                id="run_bendr_skyrimse",
                label="Run BENDr",
                description="Download BENDr from Nexus, deploy mods, and process normal maps.",
                dialog_class_path="wizards.bendr_parallaxr.BENDrWizard",
            ),
            WizardTool(
                id="run_parallaxr_skyrimse",
                label="Run ParallaxR",
                description="Download ParallaxR from Nexus, deploy mods, and process parallax textures.",
                dialog_class_path="wizards.bendr_parallaxr.ParallaxRWizard",
            ),
            WizardTool(
                id="run_skygen_skyrimse",
                label="SkyGen - Patch Generator",
                description=(
                    "Scan your load order for Base Object Swapper / SkyPatcher patch coverage "
                    "and generate new BOS or SP INI patches."
                ),
                dialog_class_path="wizards.skygen.SkyGenWizard",
                extra={"_full_width_overlay": True},
            ),
            WizardTool(
                id="run_plugin_audit_skyrimse",
                label="Plugin Audit & Cleanup",
                description=(
                    "Scan load order for safe-to-disable plugins, then disable them or clean up "
                    "orphaned SkyGen BOS/SkyPatcher INIs for plugins that must stay enabled."
                ),
                dialog_class_path="wizards.plugin_audit.PluginAuditWizard",
                extra={"_full_width_overlay": True},
            ),
        ]

    _APPDATA_SUBPATH = Path("drive_c/users/steamuser/AppData/Local/Skyrim Special Edition")
    _APPDATA_SUBPATH_GOG = Path("drive_c/users/steamuser/AppData/Local/Skyrim Special Edition GOG")
    _MYGAMES_SUBPATH = Path("Skyrim Special Edition")
    _MYGAMES_SUBPATH_GOG = Path("Skyrim Special Edition GOG")
    _CUSTOM_INI_FILENAME = "FalloutCustom.ini"
    _ARCHIVE_INI_FILENAME = "Skyrim.ini"
    _ARCHIVE_PREFS_INI_FILENAME = "SkyrimPrefs.ini"
    _PROFILE_SAVES_INI_FILES = ("Skyrim.ini", "SkyrimCustom.ini")

    @property
    def _script_extender_exe(self) -> str:
        return "skse64_loader.exe"
