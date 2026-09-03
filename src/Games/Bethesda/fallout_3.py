"""
fallout_3.py
Fallout 3 - base game handler for the Bethesda family (Data/-folder deployment).

Mod structure:
  Mods install into <game_path>/Data/
  Staged mods live in Profiles/<Game Name>/mods/

All other Bethesda-family games subclass Fallout_3.
"""

import shutil
from pathlib import Path

from Games.base_game import BaseGame, WizardTool, MODERN_DIRECTX_DEPS
from Games.Bethesda.bethesda_ini import _read_ini_key, _set_ini_key
from Utils.deployment import LinkMode, deploy_core, deploy_custom_rules, deploy_filemap, load_per_mod_strip_prefixes, load_separator_deploy_paths, expand_separator_deploy_paths, expand_separator_link_modes, expand_separator_raw_deploy, cleanup_custom_deploy_dirs, restore_custom_rules, move_to_core, restore_data_core, remove_case_alias_links, remove_probe_stub_dirs
from Utils.mods.modlist import read_modlist
from Utils.config_paths import get_profiles_dir
from Utils.vfs import ProfileVFSGameMixin

_PROFILES_DIR = get_profiles_dir()

# Games that need lavfilters for the radio music to play correctly
_LAVFILTERS_GAME_IDS = frozenset({"Fallout3", "Fallout3GOTY", "FalloutNV"})

class Fallout_3(ProfileVFSGameMixin, BaseGame):
    # This family uses one game-root plus one Data directory, matching the
    # reusable profile VFS layer model. Individual subclasses may opt out.
    vfs_legacy_setting_keys = ("skyrim_vfs_enabled",)
    vfs_physical_supports_incremental_deploy = True

    profile_overridable_settings = (
        *BaseGame.profile_overridable_settings,
        *ProfileVFSGameMixin.vfs_profile_setting_keys,
        # Read old Skyrim profiles without forcing users to toggle/re-save.
        *vfs_legacy_setting_keys,
    )

    # Compatibility aliases for profiles and callers from the original
    # Skyrim-only implementation.
    @property
    def skyrim_vfs_enabled(self) -> bool:
        return self.vfs_enabled

    def set_skyrim_vfs_enabled(self, value: bool) -> None:
        self.set_vfs_enabled(value)

    # Enderal's own launcher already bootstraps its extender and overrides this.
    vfs_prefers_script_extender = True
    supports_script_extender_swap = True

    plugins_use_star_prefix = False
    plugins_include_vanilla = True
    vanilla_plugins = ["Fallout3.esm"]
    vanilla_dlc_plugins = [
        "Anchorage.esm", "ThePitt.esm", "BrokenSteel.esm",
        "PointLookout.esm", "Zeta.esm",
    ]
    synthesis_registry_name = "Fallout3"

    # Auto-install the VC++ x64 runtime + fxc2 d3dcompiler_47 on add/save for
    # every Bethesda title (inherited by all subclasses below). The modern
    # Creation Engine games genuinely need them; the older Gamebryo titles
    # don't, but installing is harmless. LAV Filters is the exception - it is
    # added only for the games in _LAVFILTERS_GAME_IDS, since it is a real
    # install (a Windows installer under Wine), not a cheap DLL drop.
    @property
    def auto_install_deps(self) -> list[str]:
        deps = list(MODERN_DIRECTX_DEPS)
        if self.game_id in _LAVFILTERS_GAME_IDS:
            deps.append("lavfilters")
        return deps

    # paths.json extras that a non-default profile may override (per-profile).
    # heroic_app_name etc. are deliberately excluded so they stay global.
    profile_overridable_paths_extras = (
        "script_extender_swap",
        "profile_ini_files",
        "profile_saves",

        "plugins_txt_filename",
    )

    # BAIN packages are authored for Bethesda games, so re-enable the
    # sub-package picker that BaseGame disables by default.
    @property
    def supports_bain(self) -> bool:
        return True

    def __init__(self):
        self._game_path: Path | None = None
        self._prefix_path: Path | None = None
        self._deploy_mode: LinkMode = LinkMode.HARDLINK
        self._staging_path: Path | None = None
        self._script_extender_swap: bool = True
        self._profile_ini_files: bool = False
        self._profile_saves: bool = False
        # In-prefix plugins.txt filename casing. None = follow the game's
        # class default (_PLUGINS_TXT_FILENAME); the user can override it in
        # the Configure Game panel (plugins.txt vs Plugins.txt).
        self._plugins_txt_filename_override: str | None = None
        self.load_paths()

    # -----------------------------------------------------------------------
    # Identity
    # -----------------------------------------------------------------------

    @property
    def name(self) -> str:
        return "Fallout 3"

    @property
    def game_id(self) -> str:
        return "Fallout3"

    @property
    def exe_name(self) -> str:
        return "Fallout3Launcher.exe"

    # Alternate launcher basenames that some store editions ship instead of
    # ``exe_name``.  Used both for auto-detection (exe_name_alts) and for the
    # script-extender launcher swap, so whichever launcher actually exists on
    # disk is detected and swapped.  Default empty so the many subclasses that
    # reuse Fallout_3 for code (Skyrim, Fallout 4, …) don't inherit a bogus
    # alt; only classes with a real alternate launcher populate it (below).
    _launcher_alts: list[str] = []

    @property
    def exe_name_alts(self) -> list[str]:
        alts = list(self._launcher_alts)
        # GOG editions of Fallout 3 / Fallout 3 GOTY ship FalloutLauncher.exe
        # instead of Fallout3Launcher.exe.  Both classes keep that exe_name
        # (every other subclass overrides it), so key the alt off it - this
        # can't leak to Skyrim/Fallout 4/etc.
        if self.exe_name == "Fallout3Launcher.exe" and \
                "FalloutLauncher.exe" not in alts:
            alts.append("FalloutLauncher.exe")
        return alts

    @property
    def plugin_extensions(self) -> list[str]:
        return [".esp", ".esl", ".esm"]

    @property
    # The game constantly asks to open Data/Data on load, which normally does not exist.
    # This causes wine to do a full directory scan each time which causes slower loading times
    # We create an empty stub of this folder to avoid this issue.
    def probe_stub_dirs(self) -> "list[str]":
        return ["Data/Data"]

    @property
    # Similar to above, The game asks to see these folders but if the casing on disk does not match what was asked for
    # then wine will also do a full directory scan, We create these folders with the correct casing to avoid this issue.
    def case_alias_dirs(self) -> "list[str]":
        return [
                "Data", 
                "Data/Textures", 
                "Data/Meshes",
                "Data/Sound",
                "Data/Interface",
                "Data/Scripts",
                "Data/Music",
                "Data/Strings",
                "Data/Grass",
                "Data/Data",
            ]

    @property
    def steam_id(self) -> str:
        return "22300"

    @property
    def nexus_game_domain(self) -> str:
        return "fallout3"
    
    @property
    def mods_dir(self) -> str:
        return "Data"

    def runtime_snapshot_exclude_dirs(self) -> set[str] | None:
        # Data/ is reverted via Data_Core; capture only files outside it.
        return {self.mods_dir}

    @property
    def mod_folder_strip_prefixes(self) -> set[str]:
        return {"Data","oblivion"}
    
    @property
    def mod_required_top_level_folders(self) -> set[str]:
        return {"skse",
                "sfse",
                "f4se",
                "nvse",
                "fose",
                "obse",
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
                "reshade-shaders",
                "shadersfx",
                "grass",
                "video",
                "source",
                "calientetools",
                "data",
                "materials",
                "tools",
                "config",
                "menus",
                "distantlod",
                "fonts",
                "facegen",
                "lodsettings",
                "lsdata",
                "strings",
                "trees",
                "asi",
                "geometries",
                "bashtags",
                "dialogueviews",
                "terrain",
                "vis",
                "programs",
                "misc",
                "particles",
                "planetdata",
                "dyndolod",
                "netscriptframework",
                "skyproc patchers",
                "src",
                }

    @property
    def mod_auto_strip_until_required(self) -> bool:
        return True

    @property
    def mod_required_file_types(self) -> set[str]:
        return {".esp", ".esl", ".esm", ".ini", ".bsa", ".ba2"}

    @property
    def mod_install_as_is_if_no_match(self) -> bool:
        return True

    @property
    def conflict_ignore_filenames(self) -> set[str]:
        return {"info.xml","*read*.txt","*.md","*.url","*.zip"}

    @property
    def conflict_ignore_foldernames(self) -> set[str]:
        return {"src"}
    
    @property
    def archive_extensions(self) -> frozenset[str]:
        # Older Bethesda games use BSA archives. Fallout 4 / Fallout 4 VR /
        # Starfield / Fallout 76 use BA2 and override this further.
        return frozenset({".bsa"})

    @property
    def loot_sort_enabled(self) -> bool:
        return True

    @property
    def loot_game_type(self) -> str:
        return "Fallout3"

    @property
    def loot_masterlist_repo(self) -> str:
        return "fallout3"

    @property
    def reshade_dll(self) -> str:
        return "d3d9.dll"

    @property
    def reshade_arch(self) -> int:
        return 32
    
    @property
    def custom_routing_rules(self) -> list:
        from Utils.deployment import CustomRule
        return [
            CustomRule(dest="", filenames=["fose_loader.exe"], flatten=True, loose_only=True),
            CustomRule(dest="", folders=["Data"], flatten=True, loose_only=True),
            CustomRule(dest="", filenames=["fose*.dll"], flatten=True, loose_only=True),
            self._saves_routing_rule([".fos"]),
                ]

    def _saves_routing_rule(self, extensions: list[str]):
        """Route loose save files into the prefix's My Games Saves folder, mirrored to the GOG variant if that folder exists."""
        from Utils.deployment import CustomRule
        gog_sub = self._MYGAMES_SUBPATH_GOG or Path(f"{self._MYGAMES_SUBPATH} GOG")
        mirrors: list[str] = []
        if self._prefix_path is not None and (self._prefix_path / self._MYGAMES_DOCS / gog_sub).is_dir():
            mirrors.append(str(self._MYGAMES_DOCS / gog_sub / "Saves"))
        return CustomRule(
            dest=str(self._MYGAMES_DOCS / self._MYGAMES_SUBPATH / "Saves"),
            extensions=extensions, flatten=True, to_prefix=True,
            mirror_dests=mirrors,
        )

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
            *self._xedit_wizard_tools(
                build="FO3Edit", id_suffix="fo3",
                nexus_url="https://www.nexusmods.com/fallout3/mods/637?tab=files",
                nexus_file_id=1000026408,
            ),
        ]

    # The latest official xEdit is now released through the xEdit Discord (not
    # Nexus) as a single multi-game build.  It ships three launchers, one per
    # game family; the per-game build name (FO4Edit/SF1Edit/TES5Edit/…) maps to
    # whichever launcher its family uses.
    _DISCORD_XEDIT_EXES: dict[str, str] = {
        "fo": "xFOEdit.exe",     # Fallout 3 / New Vegas / 4 / VR
        "sf": "xSFEdit.exe",     # Starfield
        "tes": "xTESEdit.exe",   # Oblivion / Skyrim / SSE / VR / Enderal
    }

    # The multi-game Discord launcher requires a game-mode argument to pick the
    # game (it refuses to start without one).  Map each per-game build name to
    # the mode token the launcher accepts (FNV/FO3/FO4/FO4VR/SSE/TES4/TES5/
    # TES5VR/SF1/Enderal/EnderalSE).  Enderal shares its build name with
    # Skyrim/SSE, so those callers pass ``discord_mode=`` explicitly.
    _DISCORD_XEDIT_MODES: dict[str, str] = {
        "fo3edit": "FO3", "fnvedit": "FNV", "fo4edit": "FO4",
        "fo4vredit": "FO4VR", "fo76edit": "FO76", "sf1edit": "SF1",
        "tes4edit": "TES4", "tes5edit": "TES5", "tes5vredit": "TES5VR",
        "sseedit": "SSE",
    }

    @staticmethod
    def _discord_xedit_exe(build: str) -> str:
        """Pick the Discord xEdit launcher for a per-game *build* name.

        FO3Edit/FNVEdit/FO4Edit/FO4VREdit -> xFOEdit; SF1Edit -> xSFEdit;
        TES4Edit/TES5Edit/TES5VREdit/SSEEdit -> xTESEdit.
        """
        b = build.lower()
        if b.startswith("sf"):
            return Fallout_3._DISCORD_XEDIT_EXES["sf"]
        if b.startswith(("fo", "fnv")):
            return Fallout_3._DISCORD_XEDIT_EXES["fo"]
        return Fallout_3._DISCORD_XEDIT_EXES["tes"]

    @staticmethod
    def _discord_xedit_mode(build: str) -> str:
        """Default game-mode arg for a *build* (e.g. FO4Edit -> 'FO4')."""
        return Fallout_3._DISCORD_XEDIT_MODES.get(build.lower(), build)

    @staticmethod
    def _xedit_wizard_tools(
        build: str, id_suffix: str, nexus_url: str, nexus_file_id: int = 0,
        qac: bool = True,
        discord_only: bool = False, discord_mode: str | None = None,
    ) -> list[WizardTool]:
        """Build the 'Run <xEdit>' (+ optional QAC) wizard entries for a game.

        All Bethesda games share one parametrised xEdit wizard
        (``wizards.sseedit``); only the exe name + Nexus page differ, supplied
        via ``extra``.  Plugins xEdit creates/cleans are rescued generically by
        the game's ``restore()`` (``restore_data_core`` with overwrite/staging),
        so no per-game restore code is needed.

        Every game also gets the "Discord version" of xEdit - the latest
        official build, now released through the xEdit Discord (not Nexus) as
        one multi-game download whose launcher differs per game family
        (xFOEdit/xSFEdit/xTESEdit).  Its QAC is the same exe with
        ``-quickautoclean`` (no separate build), so it is registered as its own
        wizard entry.  ``discord_only`` drops the Nexus entries entirely (used
        by games whose Nexus build was discontinued, e.g. Starfield).
        """
        exe = f"{build}.exe"
        tools: list[WizardTool] = []
        if not discord_only:
            # nexus_file_id pins the build's main file for the hands-free
            # premium fetch; 0 keeps the download step fully manual.
            nexus_extra = {"xedit_exe": exe, "nexus_url": nexus_url,
                           "nexus_file_id": nexus_file_id,
                           "display_name": build}
            tools.append(
                WizardTool(
                    id=f"run_{build.lower()}_{id_suffix}",
                    label="Run {0}", label_args=(build,),
                    description="Install {0}, deploy mods, and run {1}.",
                    description_args=(build, exe),
                    dialog_class_path="wizards.sseedit.SSEEditWizard",
                    extra=dict(nexus_extra),
                )
            )
            if qac:
                tools.append(
                    WizardTool(
                        id=f"run_{build.lower()}_qac_{id_suffix}",
                        label="Run {0} QAC", label_args=(build,),
                        description="Deploy mods and run {0}QuickAutoClean.exe.",
                        description_args=(build,),
                        dialog_class_path="wizards.sseedit.SSEEditQACWizard",
                        extra=dict(nexus_extra),
                    )
                )

        # Community Discord build (multi-game exe, downloaded off Nexus).  The
        # launcher needs a game-mode arg to select the game (see xedit_view).
        discord_exe = Fallout_3._discord_xedit_exe(build)
        mode = discord_mode or Fallout_3._discord_xedit_mode(build)
        discord_extra = {
            "xedit_exe": discord_exe,
            "display_name": "xEdit",
            "app_dir": "xEdit (Discord)",
            "discord": True,
            "discord_mode": mode,
        }
        tools.append(
            WizardTool(
                id=f"run_xedit_discord_{id_suffix}",
                label="Run xEdit (Discord version)",
                description=(
                    "Deploy mods and run {0} -{1} from the latest "
                    "xEdit build, released through the xEdit Discord."),
                description_args=(discord_exe, mode),
                dialog_class_path="wizards.sseedit.XEditDiscordWizard",
                extra=dict(discord_extra),
            )
        )
        tools.append(
            WizardTool(
                id=f"run_xedit_discord_qac_{id_suffix}",
                label="Run xEdit QAC (Discord version)",
                description=(
                    "Deploy mods and run {0} -{1} -quickautoclean "
                    "from the latest xEdit build, released through the xEdit Discord."),
                description_args=(discord_exe, mode),
                dialog_class_path="wizards.sseedit.XEditDiscordQACWizard",
                extra=dict(discord_extra),
            )
        )
        return tools

    # -----------------------------------------------------------------------
    # Paths
    # -----------------------------------------------------------------------

    def get_game_path(self) -> Path | None:
        return self._game_path

    def get_mod_data_path(self) -> Path | None:
        """Mods go into the Data/ subfolder of the game root directory."""
        if self._game_path is None:
            return None
        return self._game_path / "Data"

    def get_mod_staging_path(self) -> Path:
        if self._staging_path is not None:
            return self._staging_path / "mods"
        return _PROFILES_DIR / self.name / "mods"

    def _load_paths_extra(self, data: dict) -> None:
        self._script_extender_swap = data.get("script_extender_swap", True)
        self._profile_ini_files = data.get("profile_ini_files", False)
        self._profile_saves = data.get("profile_saves", False)
        raw_pfname = data.get("plugins_txt_filename")
        # Only treat a stored value as an override when it differs from the
        # game's class default, so a value equal to the default (which we now
        # always persist - see _save_paths_extra) doesn't masquerade as one.
        pfname = str(raw_pfname) if raw_pfname else ""
        self._plugins_txt_filename_override = (
            pfname if pfname and pfname != self._PLUGINS_TXT_FILENAME else None)

    def _save_paths_extra(self) -> dict:
        # Always emit the effective filename (never omit it): omitting it left a
        # stale value in paths.json when the user switched back to the default,
        # so the change appeared to do nothing. A concrete value here also lets
        # save_paths() pin it per-profile via profile_overridable_paths_extras.
        return {
            "script_extender_swap": self._script_extender_swap,
            "profile_ini_files":    self._profile_ini_files,
            "profile_saves":        self._profile_saves,
            "plugins_txt_filename": self.plugins_txt_filename,
        }

    def set_staging_path(self, path: "Path | str | None") -> None:
        self._staging_path = Path(path) if path else None
        self.save_paths()

    def get_prefix_path(self) -> Path | None:
        return self._prefix_path

    def get_deploy_mode(self) -> LinkMode:
        return self._deploy_mode

    def set_deploy_mode(self, mode: LinkMode) -> None:
        self._deploy_mode = mode
        self.save_paths()

    @property
    def script_extender_swap(self) -> bool:
        return self._script_extender_swap

    def set_script_extender_swap(self, value: bool) -> None:
        self._script_extender_swap = value
        self.save_paths()

    @property
    def profile_ini_files(self) -> bool:
        return self._profile_ini_files

    def set_profile_ini_files(self, value: bool) -> None:
        self._profile_ini_files = value
        self.save_paths()
        if value:
            # Create the (empty) "ini files" folder in every profile so the
            # user has an obvious place to drop their per-profile INIs.
            self._ensure_profile_ini_dirs()

    # Name of the subfolder inside each profile that holds the user's INIs.
    _PROFILE_INI_SUBDIR = "ini files"

    def _profile_ini_dir(self, profile: str) -> Path:
        """Folder inside a profile that the user drops per-profile INIs into."""
        return self.get_profile_root() / "profiles" / profile / self._PROFILE_INI_SUBDIR

    def _ensure_profile_ini_dirs(self) -> None:
        """Create the empty 'ini files' folder for every existing profile."""
        profiles_root = self.get_profile_root() / "profiles"
        if not profiles_root.is_dir():
            return
        for profile_dir in profiles_root.iterdir():
            if profile_dir.is_dir():
                (profile_dir / self._PROFILE_INI_SUBDIR).mkdir(parents=True, exist_ok=True)

    @property
    def profile_saves(self) -> bool:
        return self._profile_saves and self.supports_profile_saves

    def set_profile_saves(self, value: bool) -> None:
        self._profile_saves = value and self.supports_profile_saves
        self.save_paths()
        if self._profile_saves:
            # Create the empty Saves folder up-front so the user knows where to
            # drop their saves, without waiting for a deploy. Seed every
            # existing profile (and the active one) since any of them may be
            # deployed next.
            self._ensure_profile_saves_dirs()

    def _ensure_profile_saves_dirs(self) -> None:
        """Create an empty ``Saves`` folder in each existing profile folder."""
        try:
            profiles_root = self.get_profile_root() / "profiles"
        except Exception:
            return
        names: set[str] = set()
        if self._active_profile_dir is not None:
            names.add(self._active_profile_dir.name)
        if profiles_root.is_dir():
            names.update(p.name for p in profiles_root.iterdir() if p.is_dir())
        for name in names:
            try:
                (profiles_root / name / self._SAVES_FOLDER_NAME).mkdir(
                    parents=True, exist_ok=True)
            except OSError:
                pass

    def set_prefix_path(self, path: Path | str | None) -> None:
        self._prefix_path = Path(path) if path else None
        self.save_paths()

    # -----------------------------------------------------------------------
    # Deployment
    # -----------------------------------------------------------------------

    _APPDATA_SUBPATH = Path("drive_c/users/steamuser/AppData/Local/Fallout3")
    # GOG subpaths are None on subclasses without a GOG release.
    _APPDATA_SUBPATH_GOG: "Path | None" = Path("drive_c/users/steamuser/AppData/Local/Fallout3 GOG")
    _MYGAMES_SUBPATH = Path("Fallout3")
    _MYGAMES_SUBPATH_GOG: "Path | None" = Path("Fallout3 GOG")
    _ARCHIVE_INI_FILENAME = "FALLOUT.INI"
    # Per-game Prefs INI. When set, archive invalidation writes the same keys
    # to both the primary INI and the Prefs INI so the Prefs file can't silently
    # override what we wrote - the engine reads both and the Prefs value wins
    # when present in both. Set to None on subclasses without a Prefs INI.
    _ARCHIVE_PREFS_INI_FILENAME: "str | None" = "FalloutPrefs.ini"

    # Whether the SArchiveList / SInvalidationFile edits go to the Prefs INI too.
    # FO3/FNV: yes - FalloutPrefs.ini legitimately carries Archive keys that
    # override Fallout.ini. Oblivion: NO - OblivionPrefs.ini does not manage
    # SArchiveList, and writing a partial list there (dummy + mod BSAs but no
    # vanilla archives, since the vanilla list only lives in Oblivion.ini)
    # shadows the good list and breaks BSA loading for ALL mods. bInvalidate-
    # OlderFiles still goes to both regardless.
    _archive_list_in_prefs_ini: bool = True
    archive_invalidation_enabled = True
    _archive_invalidation_extra_keys: tuple[tuple[str, str], ...] = ()

    # MO2-style dummy-BSA invalidation. When _invalidation_bsa_name is set, the
    # apply step writes an empty BSA into the game's Data folder, prepends it to
    # SArchiveList, and empties SInvalidationFile (disabling the legacy .txt
    # codepath). When None, only the bInvalidateOlderFiles INI key is touched.
    # BA2-based games (Fallout 4, Starfield) must override with None.
    _invalidation_bsa_name: "str | None" = "Fallout - Invalidation.bsa"
    _invalidation_bsa_version: "int | None" = 0x68
    _invalidation_archive_list_key: str = "SArchiveList"

    # FO3/FNV only: these engines read files only from BSAs listed in
    # SArchiveList - a mod BSA named to match its plugin is NOT reliably
    # auto-loaded. When True, the invalidation step appends every deployed
    # mod-provided BSA to SArchiveList so its assets load. Fallout_3/
    # Fallout3_GOTY/Fallout_NV set it True; later engines override it False.
    # Oblivion does NOT use this - it auto-loads a mod's BSA via plugin-name
    # association, and forcing entries here both fights SkyBSA's load-order
    # reversal and blows the 256-char SArchiveList limit. See
    # geckwiki.com/index.php/BSA_Files.
    _archive_list_needs_mod_bsas: bool = True

    # Engine-fix plugin whose FalloutCustom.ini support bypasses the vanilla
    # 255-char SArchiveList read limit (settings applied in-memory, 16 KB
    # buffer). FO3: Command Extender; FNV overrides with JIP LN NVSE (which
    # additionally patches the vanilla Fallout.ini read).
    _archive_list_fix_name: "str | None" = "Command Extender"
    _archive_list_fix_path: "str | None" = "Data/FOSE/Plugins/CommandExtender.dll"
    _CUSTOM_INI_FILENAME = "FalloutCustom.ini"

    @property
    def _script_extender_exe(self) -> str:
        return "fose_loader.exe"

    @property
    def frameworks(self) -> dict[str, str]:
        fw = {"Script Extender": self._script_extender_exe}
        # The SArchiveList fix plugin is only relevant on games where we
        # append mod BSAs (FO3/GOTY/FNV) - later engines inherit the attrs
        # but never hit that codepath.
        if (self._archive_list_needs_mod_bsas
                and self._archive_list_fix_name and self._archive_list_fix_path):
            fw[self._archive_list_fix_name] = self._archive_list_fix_path
        return fw

    @property
    def framework_launch_exes(self) -> dict[str, str]:
        # The script extender loader launches the game, so surface it in the
        # play-bar Run dropdown when installed. Keyed off `frameworks` so
        # subclasses that clear it (FO76) opt out automatically.
        if "Script Extender" in self.frameworks:
            return {"Script Extender": self._script_extender_exe}
        return {}

    _PLUGINS_TXT_FILENAME = "plugins.txt"

    # Whether this game reads an in-prefix plugins.txt at all. FO76 and the
    # like set this False so the Configure Game panel hides the casing option.
    uses_plugins_txt = True

    @property
    def plugins_txt_filename(self) -> str:
        """The in-prefix plugins.txt filename the game reads.

        Follows the per-game class default unless the user has overridden the
        casing in the Configure Game panel."""
        return self._plugins_txt_filename_override or self._PLUGINS_TXT_FILENAME

    def set_plugins_txt_filename(self, value: str) -> None:
        """Override the in-prefix plugins.txt filename casing and persist it.

        Passing a value that matches the game's default clears the override so
        the game keeps following its default afterwards."""
        value = (value or "").strip() or self._PLUGINS_TXT_FILENAME
        if value == self._PLUGINS_TXT_FILENAME:
            self._plugins_txt_filename_override = None
        else:
            self._plugins_txt_filename_override = value
        self.save_paths()

    # GOG builds of Bethesda games can't read a *symlinked* plugins.txt, so we
    # deploy a real copy (see Utils.plugins.deploy_plugins_copy). Casing follows
    # plugins_txt_filename (the game default - lowercase for most, Plugins.txt
    # for Oblivion/Oblivion Remastered/Starfield - or the user's override).
    def _plugins_txt_targets(self, prefix_root: "Path | None" = None) -> list[Path]:
        """Return every in-prefix path where the game might expect plugins.txt.

        Steam and GOG builds use separate AppData folders. If both exist, we
        write to both so either build picks up the load order.

        prefix_root overrides the game's own pfx/ dir - used for per-tool
        Proton prefixes (PGPatcher etc.) that need the same layout.
        """
        root = prefix_root if prefix_root is not None else self._prefix_path
        if root is None:
            return []
        fname = self.plugins_txt_filename
        steam_dir = root / self._APPDATA_SUBPATH
        targets: list[Path] = []
        if steam_dir.is_dir():
            targets.append(steam_dir / fname)
        if self._APPDATA_SUBPATH_GOG is not None:
            gog_dir = root / self._APPDATA_SUBPATH_GOG
            if gog_dir.is_dir():
                targets.append(gog_dir / fname)
        if not targets:
            targets.append(steam_dir / fname)
        return targets

    def _plugins_txt_target(self) -> Path | None:
        """Return the primary in-prefix plugins.txt path (back-compat shim)."""
        targets = self._plugins_txt_targets()
        return targets[0] if targets else None

    def _symlink_plugins_txt(self, profile: str, log_fn, prefix_root: "Path | None" = None) -> None:
        """Deploy the active profile's plugins.txt into the Proton prefix as a real copy.

        A copy (not a symlink) is required for GOG builds. The prefix is
        case-insensitive, so a single file resolves under either casing.
        """
        from Utils.plugins import deploy_plugins_copy
        _log = log_fn
        targets = self._plugins_txt_targets(prefix_root)
        if not targets:
            _log("  WARN: Prefix path not set - skipping plugins.txt deploy.")
            return

        source = self.get_profile_root() / "profiles" / profile / "plugins.txt"
        if not source.is_file():
            _log(f"  WARN: plugins.txt not found at {source} - skipping deploy.")
            return

        content = source.read_text(encoding="utf-8")
        for target in targets:
            deploy_plugins_copy(target.parent, target.name, content, _log)
            if self._lock_plugins_txt:
                # Mark read-only so Fallout 4's AE launcher can't rewrite it on
                # launch. Restore deletes the file (unlink ignores the read-only
                # bit), so the next deploy writes a fresh copy - no need to clear
                # the flag first.
                try:
                    target.chmod(0o444)
                except OSError as exc:
                    _log(f"  WARN: could not set {target.name} read-only: {exc}")

    def _remove_plugins_txt_symlink(self, log_fn) -> None:
        """Remove the deployed plugins.txt copy (or legacy symlink) on restore."""
        from Utils.plugins import remove_plugins_copy
        _log = log_fn
        for target in self._plugins_txt_targets():
            remove_plugins_copy(target.parent, target.name, _log)

    # -----------------------------------------------------------------------
    # Timestamp load order (Oblivion/FO3/FNV)
    # -----------------------------------------------------------------------

    # The legacy engine orders plugins by Data/ file mtime - plugins.txt only
    # selects the active set. Skyrim-era subclasses (plugins.txt-ordered)
    # override this back to False.
    _plugin_load_order_by_mtime: bool = True

    # Every Bethesda engine loads master-flagged plugins before non-masters.
    plugins_master_block = True

    # When True, the deployed plugins.txt is marked read-only. Only Fallout 4
    # needs this (its AE launcher rewrites the file on launch); every other
    # Bethesda game leaves it writable.
    _lock_plugins_txt = False

    def _orders_plugins_by_mtime(self) -> bool:
        return self._plugin_load_order_by_mtime and not self.plugins_use_star_prefix

    def stamp_plugin_load_order(self, profile: str, log_fn=None) -> None:
        """Set ascending mtimes on deployed plugins to match the profile's load order."""
        _log = log_fn or (lambda _: None)
        if self._game_path is None or not self._orders_plugins_by_mtime():
            return
        from Utils.plugins import read_loadorder, read_plugins
        profile_dir = self.get_profile_root() / "profiles" / profile
        ordered = read_loadorder(profile_dir / "loadorder.txt")
        if not ordered:
            ordered = [
                e.name for e in read_plugins(
                    profile_dir / "plugins.txt",
                    star_prefix=self.plugins_use_star_prefix,
                )
            ]
        if not ordered:
            return
        from Utils.plugins.mtimes import stamp_plugin_load_order
        data_dir = self._game_path / "Data"
        if self.vfs_launch_enabled:
            from Utils.vfs import virtual_data_write_path
            data_dir = virtual_data_write_path(self, ".")
        stamped = stamp_plugin_load_order(
            ordered,
            data_dir,
            staging_root=self.get_effective_mod_staging_path(),
            overwrite_dir=self.get_effective_overwrite_path(),
            log_fn=_log,
        )
        if stamped:
            _log(f"  Set mtimes on {stamped} plugin(s) to enforce load order.")

    # -----------------------------------------------------------------------
    # Archive invalidation
    # -----------------------------------------------------------------------

    _MYGAMES_DOCS = Path("drive_c/users/steamuser/Documents/My Games")

    def _get_archive_ini_path(self) -> "Path | None":
        """Return the primary INI used for archive invalidation (back-compat)."""
        mygames = self._mygames_path()
        if mygames is None:
            return None
        return mygames / self._ARCHIVE_INI_FILENAME

    def _get_archive_ini_paths(self) -> list[Path]:
        """Return every INI that needs the invalidation keys written.

        Includes the primary Fallout.ini-style INI and, when set, the Prefs INI
        in the same directory. Empty when the prefix is unconfigured.
        """
        mygames = self._mygames_path()
        if mygames is None:
            return []
        paths = [mygames / self._ARCHIVE_INI_FILENAME]
        if self._ARCHIVE_PREFS_INI_FILENAME:
            paths.append(mygames / self._ARCHIVE_PREFS_INI_FILENAME)
        return paths

    def _mygames_paths(self) -> list[Path]:
        """Return every My Games folder for this game inside the prefix.

        Steam and GOG builds use separate folders. If both exist, we manage
        both so either build sees the active profile's INIs.
        """
        if self._prefix_path is None:
            return []
        steam_dir = self._prefix_path / self._MYGAMES_DOCS / self._MYGAMES_SUBPATH
        paths: list[Path] = []
        if steam_dir.is_dir():
            paths.append(steam_dir)
        if self._MYGAMES_SUBPATH_GOG is not None:
            gog_dir = self._prefix_path / self._MYGAMES_DOCS / self._MYGAMES_SUBPATH_GOG
            if gog_dir.is_dir():
                paths.append(gog_dir)
        if not paths:
            paths.append(steam_dir)
        return paths

    def _mygames_path(self) -> "Path | None":
        """Return the primary My Games folder (back-compat shim)."""
        paths = self._mygames_paths()
        return paths[0] if paths else None

    def _symlink_profile_ini_files(self, profile: str, log_fn) -> None:
        """Symlink *.ini files from the profile folder into the My Games directory.

        Any existing file at the target is backed up as <name>.bak before being
        replaced.  Existing symlinks pointing to our profile dir are silently
        replaced without a backup (they are already managed by us).
        """
        _log = log_fn
        if not self._profile_ini_files:
            return
        mygames_dirs = self._mygames_paths()
        if not mygames_dirs:
            _log("  WARN: Prefix path not set - skipping profile INI symlinks.")
            return
        ini_dir = self._profile_ini_dir(profile)
        ini_dir.mkdir(parents=True, exist_ok=True)
        ini_files = list(ini_dir.glob("*.ini"))
        if not ini_files:
            _log(f"  No *.ini files found in '{ini_dir.name}' folder - skipping.")
            return
        for mygames in mygames_dirs:
            mygames.mkdir(parents=True, exist_ok=True)
            for src in ini_files:
                target = mygames / src.name
                if target.is_symlink():
                    target.unlink()
                elif target.exists():
                    backup = target.with_suffix(".bak")
                    target.rename(backup)
                    _log(f"  Backed up {target.name} → {backup.name}")
                target.symlink_to(src)
                _log(f"  Linked {src.name} → {target}")

    def _remove_profile_ini_symlinks(self, profile: str, log_fn) -> None:
        """Remove profile INI symlinks from My Games and restore any backups."""
        _log = log_fn
        if not self._profile_ini_files:
            return
        mygames_dirs = [p for p in self._mygames_paths() if p.is_dir()]
        if not mygames_dirs:
            return
        ini_dir = self._profile_ini_dir(profile)
        if not ini_dir.is_dir():
            return
        try:
            ini_dir_resolved = ini_dir.resolve()
        except OSError:
            ini_dir_resolved = ini_dir
        for mygames in mygames_dirs:
            # Scan the actual My Games folder so orphaned symlinks (whose source
            # .ini was deleted from the profile) are still removed.
            for target in mygames.glob("*.ini"):
                if not target.is_symlink():
                    continue
                # Compare the symlink's *target* directory against our ini dir,
                # resolving both sides so a symlinked prefix/staging path on the
                # way to ini_dir doesn't break the match.
                try:
                    link_target = target.readlink()
                    if not link_target.is_absolute():
                        link_target = target.parent / link_target
                    link_parent = link_target.resolve().parent
                except OSError:
                    continue
                if link_parent != ini_dir_resolved:
                    continue
                target.unlink()
                _log(f"  Removed profile INI symlink: {target.name}")
                backup = target.with_suffix(".bak")
                if backup.exists():
                    backup.rename(target)
                    _log(f"  Restored {target.name} from .bak")

    # -----------------------------------------------------------------------
    # Profile-specific saves
    # -----------------------------------------------------------------------
    #
    # Whether this game exposes the profile-specific saves option at all. Off
    # for games with server-side/cloud saves (e.g. Fallout 76) where there is
    # no local Saves folder worth redirecting.
    supports_profile_saves = True
    # Folder name the engine reads saves from, inside each save-link target.
    # Override on a subclass whose engine uses a different name.
    _SAVES_FOLDER_NAME = "Saves"
    # Suffix used to hide a pre-existing real Saves folder so the game can't
    # see it while our profile symlink is active.
    _SAVES_BACKUP_SUFFIX = "_backup_amm"

    def _saves_link_targets(self) -> list[Path]:
        """Return every directory that should receive a ``Saves`` symlink.

        Defaults to the game's My Games folder(s). Games whose saves live
        somewhere else can override this to point at their own location while
        reusing the deploy/restore logic below.
        """
        return self._mygames_paths()

    def _profile_saves_dir(self, profile: str) -> Path:
        """Path to the profile-specific saves folder (created on demand)."""
        return self.get_profile_root() / "profiles" / profile / self._SAVES_FOLDER_NAME

    def _symlink_profile_saves(self, profile: str, log_fn) -> None:
        """Symlink each target's ``Saves`` folder to the profile saves folder.

        Creates the profile saves folder if it does not yet exist. Any real
        (non-symlink) ``Saves`` folder already present at a target is renamed
        to ``Saves<backup-suffix>`` so the game stops seeing it; restore puts
        it back. Symlinks we already manage are replaced without a backup.
        """
        _log = log_fn
        if not self.profile_saves:
            return
        targets = self._saves_link_targets()
        if not targets:
            _log("  WARN: No save-link target - skipping profile saves.")
            return
        profile_saves = self._profile_saves_dir(profile)
        profile_saves.mkdir(parents=True, exist_ok=True)
        for target_dir in targets:
            target_dir.mkdir(parents=True, exist_ok=True)
            link = target_dir / self._SAVES_FOLDER_NAME
            if link.is_symlink():
                link.unlink()
            elif link.exists():
                backup = target_dir / (self._SAVES_FOLDER_NAME + self._SAVES_BACKUP_SUFFIX)
                if backup.exists():
                    _log(f"  WARN: {backup.name} already exists - leaving "
                         f"{link.name} in place, skipping.")
                    continue
                link.rename(backup)
                _log(f"  Backed up existing {link.name} → {backup.name}")
            link.symlink_to(profile_saves)
            _log(f"  Linked {link} → {profile_saves}")

    def _remove_profile_saves_symlinks(self, profile: str, log_fn) -> None:
        """Remove profile saves symlinks and restore any backed-up Saves folder."""
        _log = log_fn
        if not self.profile_saves:
            return
        profile_saves = self._profile_saves_dir(profile)
        for target_dir in self._saves_link_targets():
            if not target_dir.is_dir():
                continue
            link = target_dir / self._SAVES_FOLDER_NAME
            if link.is_symlink() and Path(link.resolve()) == profile_saves.resolve():
                link.unlink()
                _log(f"  Removed profile saves symlink: {link}")
            elif link.exists():
                # Not our symlink - leave it alone and skip restoring the backup
                # so we don't clobber whatever is there now.
                continue
            backup = target_dir / (self._SAVES_FOLDER_NAME + self._SAVES_BACKUP_SUFFIX)
            if backup.exists() and not link.exists():
                backup.rename(link)
                _log(f"  Restored {link.name} from {backup.name}")

    def apply_archive_invalidation(self, log_fn) -> None:
        """Set bInvalidateOlderFiles=1 in every managed game INI so loose files win.

        When ``_invalidation_bsa_name`` is set (MO2-style), also write a dummy
        BSA into the game's Data folder, prepend it to ``SArchiveList``, and
        empty ``SInvalidationFile`` to disable the legacy .txt codepath.

        Writes to both Fallout.ini and FalloutPrefs.ini (or the per-game
        equivalents) because the engine reads both at launch and the Prefs
        value wins when a key appears in both - leaving Prefs unmanaged would
        silently override what we wrote to the primary INI.
        """
        _log = log_fn
        if not self.archive_invalidation_enabled:
            return
        # AI toggled off in the GUI: ensure on-disk state matches by running
        # the revert path. Idempotent - if nothing was previously applied the
        # helpers no-op. Without this, turning AI off and re-deploying would
        # leave the dummy BSA and INI keys in place.
        if not self.archive_invalidation:
            self.revert_archive_invalidation(_log)
            return
        ini_paths = self._get_archive_ini_paths()
        if not ini_paths:
            _log("  WARN: Prefix path not set - skipping archive invalidation.")
            return

        # FO3/FNV: resolve the mod-BSA delta once so every INI gets the same
        # update and the tracking sidecar is written exactly once afterwards.
        prev_mod_bsas: list[str] = []
        new_mod_bsas: list[str] = []
        if self._archive_list_needs_mod_bsas:
            prev_mod_bsas = self._tracked_mod_bsas()
            new_mod_bsas = self._deployed_mod_bsas()

        self._write_dummy_bsa_file(_log)
        primary_ini = ini_paths[0]
        longest_list = ""
        for ini_path in ini_paths:
            ini_path.parent.mkdir(parents=True, exist_ok=True)
            _set_ini_key(ini_path, "Archive", "bInvalidateOlderFiles", "1")
            for key, value in self._archive_invalidation_extra_keys:
                if _read_ini_key(ini_path, "Archive", key) is not None:
                    continue
                _set_ini_key(ini_path, "Archive", key, value)
            # SArchiveList / SInvalidationFile only go to the Prefs INI when the
            # engine treats it as an Archive-key override (FO3/FNV). On Oblivion
            # they must stay in Oblivion.ini only - see _archive_list_in_prefs_ini.
            # Also strip any partial SArchiveList a prior version wrote to Prefs,
            # since it shadows the good list and breaks BSA loading.
            if ini_path != primary_ini and not self._archive_list_in_prefs_ini:
                self._strip_archive_list_keys(ini_path)
                continue
            written = self._apply_dummy_bsa_invalidation_ini(
                ini_path, prev_mod_bsas, new_mod_bsas)
            if len(written) > len(longest_list):
                longest_list = written

        if self._archive_list_needs_mod_bsas:
            self._save_tracked_mod_bsas(new_mod_bsas)
            if new_mod_bsas:
                _log(f"  Registered {len(new_mod_bsas)} mod BSA(s) in "
                     f"{self._invalidation_archive_list_key}.")
            self._sync_archive_list_custom_ini(ini_paths, longest_list, _log)

        names = ", ".join(p.name for p in ini_paths)
        _log(f"  Archive invalidation enabled in {names}.")

    def _sync_archive_list_custom_ini(
        self, ini_paths: "list[Path]", list_str: str, _log,
    ) -> None:
        """Route an over-limit SArchiveList through FalloutCustom.ini, or warn.

        Vanilla FO3/FNV read the key into a 255-char buffer; anything past
        that is silently truncated mid-name. JIP LN NVSE (FNV) / Command
        Extender (FO3) apply FalloutCustom.ini settings directly in memory
        with a 16 KB buffer, bypassing the limit - so when the list is over
        and the plugin is installed, mirror it there. Otherwise remove our
        key so a stale FalloutCustom.ini value can't shadow the managed INIs
        (those plugins apply it *after* the vanilla INIs load).
        """
        key = self._invalidation_archive_list_key
        ini_dirs = {p.parent for p in ini_paths}
        over = len(list_str) > 255
        if over and self._archive_list_fix_installed():
            for d in ini_dirs:
                _set_ini_key(d / self._CUSTOM_INI_FILENAME, "Archive",
                             key, list_str)
            _log(f"  {key} is {len(list_str)} chars (engine limit 255) - "
                 f"wrote full list to {self._CUSTOM_INI_FILENAME} "
                 f"({self._archive_list_fix_name} installed).")
            return
        for d in ini_dirs:
            custom_ini = d / self._CUSTOM_INI_FILENAME
            if custom_ini.is_file():
                _set_ini_key(custom_ini, "Archive", key, None)
        if over:
            fix = (f" Install {self._archive_list_fix_name} to fix this."
                   if self._archive_list_fix_name else "")
            _log(f"  WARN: {key} is {len(list_str)} characters - the engine "
                 "reads only the first 255 and some mod BSAs will not load."
                 f"{fix}")
            self.add_deploy_warning(
                f"{key} exceeds the engine's 255-character limit - some mod "
                f"BSAs will not load.{fix}")

    def revert_archive_invalidation(self, log_fn) -> None:
        """Remove the invalidation keys from every managed game INI.

        Also undoes the MO2-style dummy-BSA setup when ``_invalidation_bsa_name``
        is set: removes the BSA from ``SArchiveList`` in each INI, restores
        ``SInvalidationFile`` to its default, and deletes the dummy file.

        Not gated on the current ``archive_invalidation`` setting - revert cleans
        whatever artifacts are present so toggling the setting and re-deploying
        leaves a consistent on-disk state.
        """
        _log = log_fn
        if not self.archive_invalidation_enabled:
            return
        ini_paths = [p for p in self._get_archive_ini_paths() if p.is_file()]
        if not ini_paths:
            return

        for ini_path in ini_paths:
            self._revert_dummy_bsa_invalidation_ini(ini_path)
            _set_ini_key(ini_path, "Archive", "bInvalidateOlderFiles", None)
            for key, value in self._archive_invalidation_extra_keys:
                current = _read_ini_key(ini_path, "Archive", key)
                if current is None or current != value:
                    continue
                _set_ini_key(ini_path, "Archive", key, None)

        self._delete_dummy_bsa_file(_log)
        if self._archive_list_needs_mod_bsas:
            self._save_tracked_mod_bsas([])
            for d in {p.parent for p in ini_paths}:
                custom_ini = d / self._CUSTOM_INI_FILENAME
                if custom_ini.is_file():
                    _set_ini_key(custom_ini, "Archive",
                                 self._invalidation_archive_list_key, None)
        names = ", ".join(p.name for p in ini_paths)
        _log(f"  Archive invalidation reverted in {names}.")

    # (section, key, value) triples forced into the game's INIs on every deploy,
    # independent of archive invalidation. Removed again on restore. Used by
    # Fallout 4 to set [Bethesda.net] bEnablePlatform=0, which stops the AE
    # launcher's Creations/Bethesda.net sync from rewriting plugins.txt.
    _ini_override_keys: tuple[tuple[str, str, str], ...] = ()

    def apply_ini_overrides(self, log_fn) -> None:
        """Force ``_ini_override_keys`` into every managed game INI.

        Unlike archive invalidation, this is not gated on any setting - the
        keys are written on every deploy and always set to our value (an
        existing user value is overwritten, since the whole point is to
        override it).
        """
        if not self._ini_override_keys:
            return
        _log = log_fn
        ini_paths = self._get_archive_ini_paths()
        if not ini_paths:
            return
        for ini_path in ini_paths:
            ini_path.parent.mkdir(parents=True, exist_ok=True)
            for section, key, value in self._ini_override_keys:
                if _read_ini_key(ini_path, section, key) == value:
                    continue
                _set_ini_key(ini_path, section, key, value)
        names = ", ".join(p.name for p in ini_paths)
        _log(f"  Applied INI overrides in {names}.")

    def revert_ini_overrides(self, log_fn) -> None:
        """Remove any ``_ini_override_keys`` this game previously wrote.

        Only removes a key whose current value still matches what we wrote, so
        a value the user has since changed by hand is left untouched.
        """
        if not self._ini_override_keys:
            return
        _log = log_fn
        ini_paths = [p for p in self._get_archive_ini_paths() if p.is_file()]
        if not ini_paths:
            return
        for ini_path in ini_paths:
            for section, key, value in self._ini_override_keys:
                if _read_ini_key(ini_path, section, key) != value:
                    continue
                _set_ini_key(ini_path, section, key, None)
        names = ", ".join(p.name for p in ini_paths)
        _log(f"  Reverted INI overrides in {names}.")

    def _write_dummy_bsa_file(self, _log) -> None:
        """Write the dummy BSA into the game's Data folder, if configured."""
        bsa_name = self._invalidation_bsa_name
        bsa_version = self._invalidation_bsa_version
        if bsa_name is None or bsa_version is None:
            return
        if self._game_path is None:
            _log("  WARN: Game path not set - skipping dummy BSA write.")
            return
        from Utils.bsa.invalidation import write_dummy_bsa
        try:
            if self.vfs_launch_enabled:
                from Utils.vfs import virtual_data_write_path
                target = virtual_data_write_path(self, bsa_name)
            else:
                target = self._game_path / "Data" / bsa_name
            target.parent.mkdir(parents=True, exist_ok=True)
            write_dummy_bsa(target, bsa_version)
        except OSError as exc:
            _log(f"  WARN: Could not write {bsa_name}: {exc}")

    def _delete_dummy_bsa_file(self, _log) -> None:
        """Remove the dummy BSA from the game's Data folder, if present."""
        bsa_name = self._invalidation_bsa_name
        if bsa_name is None or self._game_path is None:
            return
        if self.vfs_launch_enabled:
            try:
                from Utils.vfs import virtual_data_write_path
                bsa_path = virtual_data_write_path(self, bsa_name)
            except RuntimeError:
                return
        else:
            bsa_path = self._game_path / "Data" / bsa_name
        if not bsa_path.is_file():
            return
        try:
            bsa_path.unlink()
            _log(f"  Removed dummy {bsa_name}.")
        except OSError as exc:
            _log(f"  WARN: Could not remove {bsa_name}: {exc}")

    def _apply_dummy_bsa_invalidation_ini(
        self, ini_path: Path,
        prev_mod_bsas: "list[str] | None" = None,
        new_mod_bsas: "list[str] | None" = None,
    ) -> str:
        """MO2-style INI edits for one INI: SArchiveList[0] + SInvalidationFile=''.
        Returns the archive list as written (for length checks)."""
        bsa_name = self._invalidation_bsa_name
        if bsa_name is None:
            return ""
        from Utils.bsa.invalidation import (
            ensure_in_archive_list, append_to_archive_list,
            remove_many_from_archive_list,
        )
        key = self._invalidation_archive_list_key
        current = _read_ini_key(ini_path, "Archive", key) or ""
        updated = ensure_in_archive_list(current, bsa_name)
        if self._archive_list_needs_mod_bsas:
            # FO3/FNV: only BSAs listed here have their assets read. Drop the
            # mod BSAs we previously appended, then re-append what's currently
            # deployed - so removed mods don't leave stale entries. Lists are
            # precomputed by the caller and the sidecar is written once there.
            prev = self._tracked_mod_bsas() if prev_mod_bsas is None else prev_mod_bsas
            mod_bsas = self._deployed_mod_bsas() if new_mod_bsas is None else new_mod_bsas
            updated = remove_many_from_archive_list(updated, prev)
            updated = append_to_archive_list(updated, mod_bsas)
        if updated != current:
            _set_ini_key(ini_path, "Archive", key, updated)
        _set_ini_key(ini_path, "Archive", "SInvalidationFile", "")
        return updated

    def _strip_archive_list_keys(self, ini_path: Path) -> None:
        """Remove SArchiveList / SInvalidationFile from an INI we no longer want
        to manage (the Oblivion Prefs INI). Leaves other Archive keys alone."""
        if _read_ini_key(ini_path, "Archive",
                         self._invalidation_archive_list_key) is not None:
            _set_ini_key(ini_path, "Archive",
                         self._invalidation_archive_list_key, None)
        if _read_ini_key(ini_path, "Archive", "SInvalidationFile") == "":
            _set_ini_key(ini_path, "Archive", "SInvalidationFile", None)

    def _revert_dummy_bsa_invalidation_ini(self, ini_path: Path) -> None:
        """Undo dummy-BSA INI edits for one INI. The dummy file itself is removed
        once per game dir by :meth:`_delete_dummy_bsa_file`."""
        bsa_name = self._invalidation_bsa_name
        if bsa_name is None:
            return
        from Utils.bsa.invalidation import (
            remove_from_archive_list, remove_many_from_archive_list,
        )
        key = self._invalidation_archive_list_key
        current = _read_ini_key(ini_path, "Archive", key)
        if current is not None:
            updated = remove_from_archive_list(current, bsa_name)
            if self._archive_list_needs_mod_bsas:
                updated = remove_many_from_archive_list(
                    updated, self._tracked_mod_bsas())
            if updated != current:
                _set_ini_key(ini_path, "Archive", key, updated or None)
        # Restore the engine default so a future deactivation doesn't leave
        # SInvalidationFile permanently empty.
        if _read_ini_key(ini_path, "Archive", "SInvalidationFile") == "":
            _set_ini_key(ini_path, "Archive", "SInvalidationFile",
                         "ArchiveInvalidation.txt")

    # --- FO3/FNV mod-BSA registration -------------------------------------
    # These engines read assets only from BSAs listed in SArchiveList, so every
    # deployed mod BSA must be appended. We track what we added in a sidecar so
    # revert/refresh can drop entries for mods that were since removed.

    def _archive_list_fix_installed(self) -> bool:
        """True if the engine-fix plugin from `_archive_list_fix_path` is on
        disk (case-insensitive walk from the game root)."""
        if self._archive_list_fix_path is None or self._game_path is None:
            return False
        if self.vfs_launch_enabled:
            from Utils.vfs import virtual_file
            return virtual_file(self, self._archive_list_fix_path)
        current = self._game_path
        for part in Path(self._archive_list_fix_path).parts:
            try:
                entries = {e.name.lower(): e for e in current.iterdir()}
            except OSError:
                return False
            match = entries.get(part.lower())
            if match is None:
                return False
            current = match
        return current.is_file()

    def _mod_bsa_tracking_path(self) -> "Path | None":
        try:
            return self.get_effective_filemap_path().parent / "managed_archives.txt"
        except Exception:
            return None

    def _tracked_mod_bsas(self) -> list[str]:
        """Mod BSA names we previously appended to SArchiveList, if any."""
        path = self._mod_bsa_tracking_path()
        if path is None or not path.is_file():
            return []
        try:
            return [ln.strip() for ln in path.read_text(encoding="utf-8").splitlines()
                    if ln.strip()]
        except OSError:
            return []

    def _save_tracked_mod_bsas(self, names: list[str]) -> None:
        path = self._mod_bsa_tracking_path()
        if path is None:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("\n".join(names) + ("\n" if names else ""),
                            encoding="utf-8")
        except OSError:
            pass

    def _deployed_mod_bsas(self) -> list[str]:
        """Top-level .bsa/.ba2 files deployed by mods, from the active filemap.

        Vanilla archives are already in the engine's default SArchiveList; we
        only append archives that a mod actually deploys into Data/.
        """
        from Utils.filegraph.deploy import current, legacy_rows
        if current() is None:
            return []
        names: list[str] = []
        seen: set[str] = set()
        try:
            for rel, _owner in legacy_rows():
                rel = rel.strip()
                if not rel or "/" in rel or "\\" in rel:
                    continue  # only top-level Data/ entries are loadable archives
                low = rel.lower()
                if not (low.endswith(".bsa") or low.endswith(".ba2")):
                    continue
                if low in seen:
                    continue
                seen.add(low)
                names.append(rel)
        except OSError:
            return []
        return self._order_mod_bsas_by_plugins(names)

    def _plugin_load_order(self) -> list[str]:
        """Lowercased plugin filenames in load order, from the active profile's
        plugins.txt (top = loads first, bottom = loads last / wins). Strips the
        star/asterisk activation prefix used by later games."""
        if self._active_profile_dir is None:
            return []
        # The profile-side file is always written lowercase; the plugins.txt
        # casing option only affects the in-prefix copy the game reads.
        path = self._active_profile_dir / "plugins.txt"
        if not path.is_file():
            return []
        order: list[str] = []
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                name = line.strip()
                if not name or name.startswith("#"):
                    continue
                if name.startswith("*"):
                    name = name[1:].strip()
                if name:
                    order.append(name.lower())
        except OSError:
            return []
        return order

    def _order_mod_bsas_by_plugins(self, bsa_names: list[str]) -> list[str]:
        """Order mod BSAs so the conflict winner follows plugin load order.

        FO3/FNV resolve SArchiveList conflicts as *first listed wins*, while a
        plugin lower in plugins.txt (loaded later) is meant to override earlier
        ones. So the later-loading plugin's BSA must come first → SArchiveList
        order is the reverse of plugin load order.

        Each BSA maps to a plugin by name prefix: ``<plugin-stem>[ suffix].bsa``
        hooks to ``<plugin-stem>.es[pml]``. BSAs with no matching enabled plugin
        keep their relative order and sort after all matched ones.
        """
        order = self._plugin_load_order()
        if not order:
            return bsa_names
        # plugin stem (no extension) -> load index
        stem_rank: dict[str, int] = {}
        for i, plugin in enumerate(order):
            stem = plugin.rsplit(".", 1)[0]
            stem_rank[stem] = i

        def rank(bsa: str) -> int:
            stem = bsa.rsplit(".", 1)[0].lower()
            # longest matching plugin-stem prefix (handles "Name - Textures.bsa")
            best = -1
            for pstem, idx in stem_rank.items():
                if stem == pstem or stem.startswith(pstem + " "):
                    if idx > best:
                        best = idx
            return best

        ranked = [(rank(b), i, b) for i, b in enumerate(bsa_names)]
        # Matched BSAs first, by descending plugin index (later plugin wins →
        # earlier in list); then unmatched (rank -1) in original order.
        matched = sorted((t for t in ranked if t[0] >= 0),
                         key=lambda t: (-t[0], t[1]))
        unmatched = [t for t in ranked if t[0] < 0]
        return [b for _, _, b in matched] + [b for _, _, b in unmatched]

    def _launcher_name(self) -> str:
        """The launcher filename that actually exists in the game folder.

        Some store editions ship a differently-named launcher (e.g. GOG
        Fallout 3 uses ``FalloutLauncher.exe`` instead of
        ``Fallout3Launcher.exe``).  Return the first of ``exe_name`` /
        ``exe_name_alts`` that is present on disk (or has a matching ``.bak``
        backup from a previous swap), falling back to ``exe_name``.
        """
        candidates = [self.exe_name, *self.exe_name_alts]
        if self._game_path is not None:
            for name in candidates:
                stem = Path(name).stem
                if (self._game_path / name).is_file() or \
                   (self._game_path / (stem + ".bak")).is_file():
                    return name
        return self.exe_name

    def swap_launcher(self, log_fn) -> None:
        """Replace the game launcher with the script extender if present."""
        _log = log_fn
        if self._game_path is None:
            return
        if not self.supports_script_extender_swap:
            _log("  Launcher swap is not used for this game - skipping.")
            return
        if self.vfs_launch_enabled:
            _log("  VFS launch: launcher swap is virtual - game files unchanged.")
            return
        if not self._script_extender_swap:
            _log("  Script extender / launcher swap disabled - skipping.")
            return
        se = self._game_path / self._script_extender_exe
        if not se.is_file():
            _log(f"  {self._script_extender_exe} not found - skipping launcher swap.")
            return
        exe_name = self._launcher_name()
        launcher = self._game_path / exe_name
        backup   = self._game_path / (Path(exe_name).stem + ".bak")
        if launcher.is_file():
            launcher.rename(backup)
            _log(f"  Renamed {exe_name} → {backup.name}.")
        shutil.copy2(se, launcher)
        _log(f"  Copied {self._script_extender_exe} → {exe_name}.")

    def _restore_launcher(self, log_fn) -> None:
        """Reverse the script extender launcher swap if a backup exists."""
        _log = log_fn
        if self._game_path is None:
            return
        exe_name = self._launcher_name()
        backup   = self._game_path / (Path(exe_name).stem + ".bak")
        launcher = self._game_path / exe_name
        if not backup.is_file():
            return
        if launcher.is_file():
            launcher.unlink()
        backup.rename(launcher)
        _log(f"  Restored {exe_name} from {backup.name}.")

    def deploy(self, log_fn=None, mode: LinkMode = LinkMode.HARDLINK,
               profile: str = "default", progress_fn=None) -> None:
        """Deploy staged mods into the game's Data directory.

        Workflow:
          1. Move everything currently in Data/ → Data_Core/
          2. Hard-link every file listed in filemap.txt into Data/
          3. Hard-link vanilla files from Data_Core/ into Data/ for anything
             not provided by a mod
          4. Symlink the active profile's plugins.txt into the Proton prefix
          5. Swap launcher for FOSE
        (Root Folder deployment is handled by the GUI after this returns.)
        """
        _log = log_fn or (lambda _: None)

        if self._game_path is None:
            raise RuntimeError("Game path is not configured.")

        data_dir = self._game_path / "Data"
        filemap  = self.get_effective_filemap_path()
        staging  = self.get_effective_mod_staging_path()

        if not data_dir.is_dir():
            raise RuntimeError(f"Data directory not found: {data_dir}")
        from Utils.filegraph.deploy import input_ready
        if not input_ready():
            raise RuntimeError(
                f"filemap.txt not found: {filemap}\n"
                "Run 'Build Filemap' before deploying."
            )

        if self.vfs_launch_enabled:
            return self._deploy_vfs(
                profile=profile, filemap=filemap, staging=staging,
                log_fn=_log, progress_fn=progress_fn)

        profile_dir = self.get_profile_root() / "profiles" / profile
        per_mod_strip = load_per_mod_strip_prefixes(profile_dir)

        # Per-separator deploy overrides. Loaded here (from the real profile_dir,
        # which is where modlist.txt / profile_state.json live - the filemap may
        # sit at the shared-staging profile root instead) and passed explicitly
        # to both Step 0 and Step 2 so the self-load fallbacks in those functions
        # don't have to guess the profile dir from filemap_path.parent.
        _sep_deploy = load_separator_deploy_paths(profile_dir)
        _sep_entries = read_modlist(profile_dir / "modlist.txt") if _sep_deploy else []
        per_mod_deploy = expand_separator_deploy_paths(_sep_deploy, _sep_entries) or None
        per_mod_modes = expand_separator_link_modes(_sep_deploy, _sep_entries) or None
        per_mod_raw = expand_separator_raw_deploy(_sep_deploy, _sep_entries) or None

        custom_rules = self.custom_routing_rules
        custom_exclude: set[str] = set()
        if custom_rules:
            _log("Step 0: Routing files via custom rules ...")
            custom_exclude = deploy_custom_rules(
                filemap, self._game_path, staging,
                rules=custom_rules,
                mode=mode,
                strip_prefixes=self.mod_folder_strip_prefixes,
                per_mod_strip_prefixes=per_mod_strip,
                per_mod_link_modes=per_mod_modes,
                raw_mods=per_mod_raw,
                log_fn=_log,
                progress_fn=progress_fn,
                prefix_root=self.get_prefix_path(),
            )

        _log("Step 1: Moving Data/ → Data_Core/ ...")
        move_to_core(data_dir, log_fn=_log)
        _log("  Backed up existing files → Data_Core/.")

        _log(f"Step 2: Transferring mod files into Data/ ({mode.name}) ...")
        linked_mod, placed = deploy_filemap(filemap, data_dir, staging,
                                            mode=mode,
                                            strip_prefixes=self.mod_folder_strip_prefixes,
                                            per_mod_strip_prefixes=per_mod_strip,
                                            per_mod_deploy_dirs=per_mod_deploy,
                                            per_mod_link_modes=per_mod_modes,
                                            log_fn=_log,
                                            progress_fn=progress_fn,
                                            exclude=custom_exclude or None,
                                            core_dir=data_dir.parent / (data_dir.name + "_Core"))
        _log(f"  Transferred {linked_mod} mod file(s).")

        _log("Step 3: Filling gaps with vanilla files from Data_Core/ ...")
        linked_core = deploy_core(data_dir, placed, mode=mode, log_fn=_log,
                                  manifest_dir=filemap.parent)
        _log(f"  Transferred {linked_core} vanilla file(s).")

        _log("Step 4: Symlinking plugins.txt into Proton prefix ...")
        self._symlink_plugins_txt(profile, _log)

        _log("Step 5: Symlinking profile INI files ...")
        self._symlink_profile_ini_files(profile, _log)

        _log("Step 6: Symlinking profile saves ...")
        self._symlink_profile_saves(profile, _log)

        _log("Step 7: Applying archive invalidation ...")
        self.apply_archive_invalidation(_log)

        self.apply_ini_overrides(_log)

        if self._orders_plugins_by_mtime():
            _log("Step 8: Setting plugin mtimes to match load order ...")
            self.stamp_plugin_load_order(profile, _log)

        _log(
            f"Deploy complete. "
            f"{linked_mod} mod + {linked_core} vanilla "
            f"= {linked_mod + linked_core} total file(s) in Data/."
        )

        # Capture runtime files generated outside Data/ on the next restore.
        self.snapshot_root_for_runtime_capture(log_fn=_log)

    def restore(self, log_fn=None, progress_fn=None) -> None:
        """Restore Data/ to its vanilla state by moving Data_Core/ back."""
        _log = log_fn or (lambda _: None)

        if self._game_path is None:
            raise RuntimeError("Game path is not configured.")

        data_dir = self._game_path / "Data"

        _log("Restore: removing case-alias symlinks ...")
        remove_case_alias_links(self._game_path, self.case_alias_dirs,
                                log_fn=_log)
        remove_probe_stub_dirs(self._game_path, self.probe_stub_dirs,
                               log_fn=_log)

        _log("Restore: reverting archive invalidation ...")
        self.revert_archive_invalidation(_log)

        self.revert_ini_overrides(_log)

        _profile_dir = self._active_profile_dir
        _entries = read_modlist(_profile_dir / "modlist.txt") if _profile_dir else []
        cleanup_custom_deploy_dirs(
            _profile_dir, _entries, log_fn=_log,
            filemap_path=self.get_effective_filemap_path(),
            game=self,
        )

        custom_rules = self.custom_routing_rules
        if custom_rules and self._game_path:
            _log("Restore: removing custom-routed files ...")
            restore_custom_rules(
                self.get_effective_filemap_path(),
                self._game_path,
                rules=custom_rules,
                log_fn=_log,
                prefix_root=self.get_prefix_path(),
            )

        core_dir = data_dir.parent / (data_dir.name + "_Core")
        physical_deploy = core_dir.exists()
        from Utils.vfs import cleanup_deployment, has_deployment_state
        vfs_deploy = has_deployment_state(self)
        if vfs_deploy:
            cleanup_deployment(self, preserve_upper=True, log_fn=_log)
        if physical_deploy:
            # A physical deploy may still be active after VFS was toggled on.
            _log("Restore: clearing Data/ and moving Data_Core/ back ...")
            restored = restore_data_core(
                data_dir,
                overwrite_dir=self.get_effective_overwrite_path(),
                staging_root=self.get_effective_mod_staging_path(),
                strip_prefixes=self.mod_folder_strip_prefixes,
                log_fn=_log,
                restore_whitelist=self.restore_whitelist_matcher(
                    rel_prefix="data/"),
                game=self, profile_dir=self._active_profile_dir,
            )
            _log(f"  Restored {restored} file(s). Data_Core/ removed.")
        elif not vfs_deploy:
            cleanup_deployment(self, preserve_upper=True, log_fn=_log)

        self._remove_plugins_txt_symlink(_log)
        self._restore_launcher(_log)

        # After Data/ + launcher are restored, so the launcher .bak (created by
        # swap_launcher *after* the deploy snapshot) isn't swept as a runtime file.
        if physical_deploy:
            moved = self.capture_runtime_files_to_root_folder(log_fn=_log)
            if moved:
                _log(f"  Moved {moved} runtime file(s) to Root_Folder/.")

        _active = self._active_profile_dir
        if _active is not None:
            _log("Restore: removing profile INI symlinks ...")
            self._remove_profile_ini_symlinks(_active.name, _log)
            _log("Restore: removing profile saves symlinks ...")
            self._remove_profile_saves_symlinks(_active.name, _log)

        _log("Restore complete.")
