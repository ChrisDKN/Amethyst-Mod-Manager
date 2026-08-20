"""
skyrim_se.py
Game handler for Skyrim Special Edition.

Mod structure:
  Mods install into <game_path>/Data/
  Staged mods live in Profiles/Skyrim Special Edition/mods/
"""

from pathlib import Path

from Games.Bethesda.fallout_3 import Fallout_3
from Games.base_game import WizardTool, MODERN_DIRECTX_DEPS


class SkyrimSE(Fallout_3):

    # Skyrim's BSResource loose-file traversal still encounters legacy Windows
    # path limits.  Deep OAR animation paths that are safe below the normal
    # Steam install can cross MAX_PATH when the process sees the longer profile
    # `.amethyst-vfs/view` path.  Bind the view at the configured game path so
    # Skyrim retains its short, stable working directory.
    vfs_bind_launch_at_game_root = True

    # SSE auto-loads plugin-matched BSAs - it is NOT a FO3/FNV-style engine that
    # only reads archives listed in the INI. Override the Fallout_3 default.
    _archive_list_needs_mod_bsas = False
    plugins_use_star_prefix = True
    plugins_include_vanilla = False
    # Vanilla incl. Skyrim.ccc content (and _ResourcePack.esl, which is listed
    # inside Skyrim.ccc since 1.6.1130) stays OUT of plugins.txt - the engine
    # force-loads it before reading the file and strips any such entries on
    # launch. MO2/Vortex/LOOT exclude it identically.
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

    # -----------------------------------------------------------------------
    # Identity
    # -----------------------------------------------------------------------

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
        # Skyrim SE subset - excludes Fallout-specific folders (f4se, nvse,
        # fose, config) that Fallout_3 includes.
        return {
            "skse",
            "textures",
            "sound",
            "meshes",
            "mcm",
            "scripts",
            "interface",
            "lightplacer",
            "mapmarkers",
            "music",
            "nemesis_engine",
            "seq",
            "shadercache",
            "shaders",
            "grass",
            "video",
            "source",
            "calientetools",
            "data",
            "PBRNifPatcher",
            "PBRTextureSets",
            "distantlod",
            "fonts",
            "facegen",
            "menus",
            "lodsettings",
            "lsdata",
            "strings",
            "trees",
            "asi",
            "tools",
            "enbseries",
        }

    @property
    def mod_folder_strip_prefixes_post(self) -> set[str]:
        return {"data"}

    @property
    def loot_game_type(self) -> str:
        return "SkyrimSE"

    @property
    def loot_masterlist_repo(self) -> str:
        return "skyrimse"

    @property
    def wine_dll_overrides(self) -> dict[str, str]:
        # native,builtin for the XAudio2/X3DAudio/XACT family: a pure-native
        # xaudio2_7 access-violates on the audio thread under Proton (crash in
        # XAudio2_7.dll touching BSXAudio2GameSound), so prefer native but fall
        # back to Wine's builtin (wired to winepulse). d3dcompiler_47 stays
        # native - we install the Mozilla fxc2 build that supports SM5.x typed
        # UAV loads (Community Shaders / ENB; see install_d3dcompiler_47).
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

    # SKSE-plugin DLL mods need the VC++ runtime in the prefix or they fail to
    # load silently (a common "the manager broke my DLL mods" report);
    # d3dcompiler_47 (fxc2 build) is needed for Community Shaders / ENB. Both
    # install silently on first add via the Proton-menu installers. Shared with
    # the other modern Creation Engine games (FO4/4VR, SkyrimVR, Starfield, …).
    auto_install_deps = MODERN_DIRECTX_DEPS

    @property
    def frameworks(self) -> dict[str, str]:
        return {"Script Extender": "skse64_loader.exe"}

    @property
    def framework_launch_exes(self) -> dict[str, str]:
        return {"Script Extender": "skse64_loader.exe"}

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
        from Utils.deploy import CustomRule
        return [
            CustomRule(dest="", filenames=["d3dx9_42.dll"], flatten=True),
            CustomRule(dest="", filenames=["skse64_1*.dll"], flatten=True, loose_only=True),
            CustomRule(dest="", filenames=["skse64_loader.exe"], flatten=True, loose_only=True),
            CustomRule(dest="", filenames=["d3dcompiler_47.dll"], flatten=True, loose_only=True),
            CustomRule(dest="Data/SKSE/Plugins/CharGen/Presets", extensions=[".jslot"], flatten=True),
            # ENB Series files → game root
            CustomRule(dest="", filenames=[
                "d3d11.dll",
                "d3dcompiler_46e.dll",
                "enblocal.ini",
                "enbseries.ini",
            ], flatten=True),
            CustomRule(dest="", folders=["enbseries"], flatten=True),
            self._saves_routing_rule([".ess"]),
        ]

    # -----------------------------------------------------------------------
    # ENB
    # -----------------------------------------------------------------------

    @property
    def additional_install_logic(self) -> list:
        return super().additional_install_logic + [self._patch_enblocal_linux_version]

    def _patch_enblocal_linux_version(self, dest_root: Path, mod_name: str,
                                      log_fn=None) -> None:
        """Force [GLOBAL] LinuxVersion=true in any staged enblocal.ini.

        ENB needs this to take the Wine/Proton code path; without it the binary
        assumes Windows and the game hangs or renders without effects. Patching
        at install time (not deploy) keeps the staged mod the master copy, so it
        survives redeploys and is never written through a hardlink into staging.
        """
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
        from Utils.pandora_tools import find_pandora_exe
        from Utils.wizard_gates import (
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
        if find_pandora_exe(self) is not None:
            pandora_tools.append(WizardTool(
                id="run_pandora_skyrimse",
                label="Run Pandora",
                description="Deploy mods and run Pandora Behaviour Engine+.",
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
        # Native Linux builds - always listed: the wizard downloads the
        # AppImage itself, so there is no staged exe to gate on.
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
                id="install_se_skyrimse",
                label="Install Script Extender (SKSE64)",
                description="Download and install SKSE64 into the game folder.",
                dialog_class_path="wizards.script_extender.ScriptExtenderWizard",
                extra={
                    "versions": [
                        {
                            "label": "Skyrim SE 1.6.1170 (Steam, current)",
                            "description": "Latest SKSE64 release from GitHub. Use this for up-to-date Steam installs.",
                            "github_api_url": "https://api.github.com/repos/ianpatt/skse64/releases/latest",
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
            WizardTool(
                id="run_xlodgen_skyrimse",
                label="Run xLODGen",
                description="Install xLODGen, deploy mods, and run xLODGenx64.exe.",
                dialog_class_path="wizards.dyndolod.xLODGenWizard",
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

    # -----------------------------------------------------------------------
    # Deployment
    # -----------------------------------------------------------------------

    _APPDATA_SUBPATH = Path("drive_c/users/steamuser/AppData/Local/Skyrim Special Edition")
    _APPDATA_SUBPATH_GOG = Path("drive_c/users/steamuser/AppData/Local/Skyrim Special Edition GOG")
    _MYGAMES_SUBPATH = Path("Skyrim Special Edition")
    _MYGAMES_SUBPATH_GOG = Path("Skyrim Special Edition GOG")
    _ARCHIVE_INI_FILENAME = "Skyrim.ini"
    _ARCHIVE_PREFS_INI_FILENAME = "SkyrimPrefs.ini"
    # SSE engine doesn't need the dummy-BSA trick: bUseLooseFiles defaults true
    # and the engine prefers loose files over archived assets without timestamp
    # gymnastics. MO2's game_skyrimSE plugin omits a BSAInvalidation feature
    # entirely - we match that. Only the bInvalidateOlderFiles INI key is set.
    _invalidation_bsa_name = None
    _invalidation_bsa_version = None

    @property
    def _script_extender_exe(self) -> str:
        return "skse64_loader.exe"

    # swap_launcher / _restore_launcher are inherited from Fallout_3: it
    # derives the launcher name from exe_name (SkyrimSELauncher.exe - GOG uses
    # the same name, unlike GOG Fallout 3) and the SE loader from
    # _script_extender_exe above, so the shared Bethesda implementation is
    # already correct for both physical and profile-VFS deployment.
