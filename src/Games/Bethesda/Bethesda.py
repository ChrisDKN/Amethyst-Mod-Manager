"""
Bethesda.py
Game handler for Various Bethesda games using the same deployment method.

Mod structure:
  Mods install into <game_path>/Data/
  Staged mods live in Profiles/Fallout 3/mods/
"""

import configparser
import json
import shutil
from pathlib import Path

from Games.base_game import BaseGame, WizardTool
from Utils.deploy import LinkMode, deploy_core, deploy_custom_rules, deploy_filemap, load_per_mod_strip_prefixes, load_separator_deploy_paths, expand_separator_deploy_paths, cleanup_custom_deploy_dirs, restore_custom_rules, move_to_core, restore_data_core
from Utils.modlist import read_modlist
from Utils.config_paths import get_profiles_dir
from Utils.steam_finder import find_prefix

_PROFILES_DIR = get_profiles_dir()


class Fallout_3(BaseGame):

    def __init__(self):
        self._game_path: Path | None = None
        self._prefix_path: Path | None = None
        self._deploy_mode: LinkMode = LinkMode.HARDLINK
        self._staging_path: Path | None = None
        self._symlink_plugins: bool = False
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

    @property
    def plugin_extensions(self) -> list[str]:
        return [".esp", ".esl", ".esm"]

    @property
    def steam_id(self) -> str:
        return "22300"

    @property
    def nexus_game_domain(self) -> str:
        return "fallout3"
    
    @property
    def mod_folder_strip_prefixes(self) -> set[str]:
        return {"Data"}
    
    @property
    def mod_required_top_level_folders(self) -> set[str]:
        return {"skse",
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
                "f4se",
                "materials",
                "tools",
                "nvse",
                "config",
                "menus",
                "fose",
                }

    @property
    def mod_auto_strip_until_required(self) -> bool:
        return True

    @property
    def mod_required_file_types(self) -> set[str]:
        return {".esp", ".esl", ".esm",".ini"}

    @property
    def mod_install_as_is_if_no_match(self) -> bool:
        return True

    @property
    def conflict_ignore_filenames(self) -> set[str]:
        return {"info.xml","readme.txt","*.jpg"}
    
    @property
    def loot_sort_enabled(self) -> bool:
        return True

    @property
    def loot_game_type(self) -> str:
        return "Fallout3"

    @property
    def loot_masterlist_url(self) -> str:
        return "https://raw.githubusercontent.com/loot/fallout3/v0.26/masterlist.yaml"

    @property
    def reshade_dll(self) -> str:
        return "d3d9.dll"

    @property
    def reshade_arch(self) -> int:
        return 32
    
    @property
    def custom_routing_rules(self) -> list:
        from Utils.deploy import CustomRule
        return [
            CustomRule(dest="", filenames=["fose_loader.exe"]),
            CustomRule(dest="", filenames=["fose_1_0.dll"]),
            CustomRule(dest="", filenames=["fose_1_1.dll"]),
            CustomRule(dest="", filenames=["fose_1_4.dll"]),
            CustomRule(dest="", filenames=["fose_1_4b.dll"]),
            CustomRule(dest="", filenames=["fose_1_5.dll"]),
            CustomRule(dest="", filenames=["fose_1_6.dll"]),
            CustomRule(dest="", filenames=["fose_1_7.dll"]),
            CustomRule(dest="", filenames=["fose_1_7ng.dll"]),
            CustomRule(dest="", filenames=["fose_editor_1_1.dll"]),
            CustomRule(dest="", filenames=["fose_editor_1_5.dll"]),
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
                    "download_url": "https://fose.silverlock.org/download/fose_v1_2_beta2.7z",
                    "archive_keywords": ["fose"],
                },
            ),
            WizardTool(
                id="run_wrye_bash_fo3",
                label="Run Wrye Bash",
                description="Download and run Wrye Bash.",
                dialog_class_path="wizards.wrye_bash.WryeBashWizard",
            ),
        ]

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

    # -----------------------------------------------------------------------
    # Configuration persistence
    # -----------------------------------------------------------------------

    def load_paths(self) -> bool:
        self._migrate_old_config()
        if not self._paths_file.exists():
            self._game_path = None
            self._prefix_path = None
            self._staging_path = None
            return False
        try:
            data = json.loads(self._paths_file.read_text(encoding="utf-8"))
            raw = data.get("game_path", "")
            if raw:
                self._game_path = Path(raw)
            raw_pfx = data.get("prefix_path", "")
            if raw_pfx:
                self._prefix_path = Path(raw_pfx)
            raw_mode = data.get("deploy_mode", "hardlink")
            self._deploy_mode = {
                "symlink": LinkMode.SYMLINK,
                "copy":    LinkMode.SYMLINK,
            }.get(raw_mode, LinkMode.HARDLINK)
            raw_staging = data.get("staging_path", "")
            if raw_staging:
                self._staging_path = Path(raw_staging)
            self._symlink_plugins = data.get("symlink_plugins", False)
            self._validate_staging()
            # If prefix is missing or no longer valid, scan for it and persist
            if not self._prefix_path or not self._prefix_path.is_dir():
                found = find_prefix(self.steam_id)
                if found:
                    self._prefix_path = found
                    self.save_paths()
            return bool(self._game_path)
        except (json.JSONDecodeError, OSError):
            pass
        self._game_path = None
        self._prefix_path = None
        return False

    def save_paths(self) -> None:
        self._paths_file.parent.mkdir(parents=True, exist_ok=True)
        mode_str = {
            LinkMode.SYMLINK: "symlink",
            LinkMode.COPY:    "copy",
        }.get(self._deploy_mode, "hardlink")
        data = {
            "game_path":       str(self._game_path)    if self._game_path    else "",
            "prefix_path":     str(self._prefix_path)  if self._prefix_path  else "",
            "deploy_mode":     mode_str,
            "staging_path":    str(self._staging_path) if self._staging_path else "",
            "symlink_plugins": self._symlink_plugins,
        }
        self._paths_file.write_text(
            json.dumps(data, indent=2), encoding="utf-8"
        )

    def set_game_path(self, path: Path | str | None) -> None:
        self._game_path = Path(path) if path else None
        self.save_paths()

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
    def symlink_plugins(self) -> bool:
        return self._symlink_plugins

    def set_symlink_plugins(self, value: bool) -> None:
        self._symlink_plugins = value
        self.save_paths()

    def set_prefix_path(self, path: Path | str | None) -> None:
        self._prefix_path = Path(path) if path else None
        self.save_paths()

    # -----------------------------------------------------------------------
    # Deployment
    # -----------------------------------------------------------------------

    _APPDATA_SUBPATH = Path("drive_c/users/steamuser/AppData/Local/Fallout3")
    _MYGAMES_SUBPATH = Path("Fallout3")
    _ARCHIVE_INI_FILENAME = "FALLOUT.ini"
    archive_invalidation_enabled = True

    @property
    def _script_extender_exe(self) -> str:
        return "fose_loader.exe"

    @property
    def frameworks(self) -> dict[str, str]:
        return {"Script Extender": self._script_extender_exe}

    def _plugins_txt_target(self) -> Path | None:
        """Return the in-prefix path where Fallout 3 expects plugins.txt."""
        if self._prefix_path is None:
            return None
        return self._prefix_path / self._APPDATA_SUBPATH / "plugins.txt"

    def _symlink_plugins_txt(self, profile: str, log_fn) -> None:
        """Symlink the active profile's plugins.txt into the Proton prefix."""
        _log = log_fn
        target = self._plugins_txt_target()
        if target is None:
            _log("  WARN: Prefix path not set — skipping plugins.txt symlink.")
            return

        source = self.get_profile_root() / "profiles" / profile / "plugins.txt"
        if not source.is_file():
            _log(f"  WARN: plugins.txt not found at {source} — skipping symlink.")
            return

        # Remove whatever is currently at the target (old symlink, real file, etc.)
        if target.exists() or target.is_symlink():
            target.unlink()

        target.parent.mkdir(parents=True, exist_ok=True)
        target.symlink_to(source)
        _log(f"  Linked plugins.txt → {target}")

    def _remove_plugins_txt_symlink(self, log_fn) -> None:
        """Remove the plugins.txt symlink from the Proton prefix on restore."""
        _log = log_fn
        target = self._plugins_txt_target()
        if target is None:
            return
        if target.is_symlink():
            target.unlink()
            _log("  Removed plugins.txt symlink from prefix.")

    # -----------------------------------------------------------------------
    # Archive invalidation
    # -----------------------------------------------------------------------

    _MYGAMES_DOCS = Path("drive_c/users/steamuser/Documents/My Games")

    def _get_archive_ini_path(self) -> "Path | None":
        """Return the full path to the game INI used for archive invalidation."""
        if self._prefix_path is None:
            return None
        return (
            self._prefix_path
            / self._MYGAMES_DOCS
            / self._MYGAMES_SUBPATH
            / self._ARCHIVE_INI_FILENAME
        )

    def apply_archive_invalidation(self, log_fn) -> None:
        """Set bInvalidateOlderFiles=1 in the game INI so loose files win."""
        _log = log_fn
        if not self.archive_invalidation_enabled or not self.archive_invalidation:
            return
        ini_path = self._get_archive_ini_path()
        if ini_path is None:
            _log("  WARN: Prefix path not set — skipping archive invalidation.")
            return

        parser = configparser.RawConfigParser(strict=False)
        parser.optionxform = str  # preserve key casing
        if ini_path.is_file():
            parser.read(str(ini_path), encoding="utf-8")

        if not parser.has_section("Archive"):
            parser.add_section("Archive")
        parser.set("Archive", "bInvalidateOlderFiles", "1")

        ini_path.parent.mkdir(parents=True, exist_ok=True)
        with open(ini_path, "w", encoding="utf-8") as fh:
            parser.write(fh, space_around_delimiters=False)
        _log(f"  Archive invalidation enabled in {ini_path.name}.")

    def revert_archive_invalidation(self, log_fn) -> None:
        """Remove bInvalidateOlderFiles from the game INI."""
        _log = log_fn
        if not self.archive_invalidation_enabled or not self.archive_invalidation:
            return
        ini_path = self._get_archive_ini_path()
        if ini_path is None or not ini_path.is_file():
            return

        parser = configparser.RawConfigParser(strict=False)
        parser.optionxform = str
        parser.read(str(ini_path), encoding="utf-8")

        if parser.has_section("Archive"):
            parser.remove_option("Archive", "bInvalidateOlderFiles")
            if not parser.options("Archive"):
                parser.remove_section("Archive")

        with open(ini_path, "w", encoding="utf-8") as fh:
            parser.write(fh, space_around_delimiters=False)
        _log(f"  Archive invalidation reverted in {ini_path.name}.")

    def swap_launcher(self, log_fn) -> None:
        """Replace the game launcher with the script extender if present."""
        _log = log_fn
        if self._game_path is None:
            return
        se = self._game_path / self._script_extender_exe
        if not se.is_file():
            _log(f"  {self._script_extender_exe} not found — skipping launcher swap.")
            return
        launcher = self._game_path / self.exe_name
        backup   = self._game_path / (Path(self.exe_name).stem + ".bak")
        if launcher.is_file():
            launcher.rename(backup)
            _log(f"  Renamed {self.exe_name} → {backup.name}.")
        shutil.copy2(se, launcher)
        _log(f"  Copied {self._script_extender_exe} → {self.exe_name}.")

    def _restore_launcher(self, log_fn) -> None:
        """Reverse the script extender launcher swap if a backup exists."""
        _log = log_fn
        if self._game_path is None:
            return
        backup   = self._game_path / (Path(self.exe_name).stem + ".bak")
        launcher = self._game_path / self.exe_name
        if not backup.is_file():
            return
        if launcher.is_file():
            launcher.unlink()
        backup.rename(launcher)
        _log(f"  Restored {self.exe_name} from {backup.name}.")

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
        if not filemap.is_file():
            raise RuntimeError(
                f"filemap.txt not found: {filemap}\n"
                "Run 'Build Filemap' before deploying."
            )

        custom_rules = self.custom_routing_rules
        custom_exclude: set[str] = set()
        if custom_rules:
            _log("Step 0: Routing files via custom rules ...")
            custom_exclude = deploy_custom_rules(
                filemap, self._game_path, staging,
                rules=custom_rules,
                mode=mode,
                strip_prefixes=self.mod_folder_strip_prefixes,
                log_fn=_log,
                progress_fn=progress_fn,
            )

        _log("Step 1: Moving Data/ → Data_Core/ ...")
        moved = move_to_core(data_dir, log_fn=_log)
        _log(f"  Moved {moved} file(s) to Data_Core/.")

        _log(f"Step 2: Transferring mod files into Data/ ({mode.name}) ...")
        profile_dir = self.get_profile_root() / "profiles" / profile
        per_mod_strip = load_per_mod_strip_prefixes(profile_dir)
        _sep_deploy = load_separator_deploy_paths(profile_dir)
        _sep_entries = read_modlist(profile_dir / "modlist.txt") if _sep_deploy else []
        per_mod_deploy = expand_separator_deploy_paths(_sep_deploy, _sep_entries) or None
        _symlink_exts = set(self.plugin_extensions) if self._symlink_plugins else None
        linked_mod, placed = deploy_filemap(filemap, data_dir, staging,
                                            mode=mode,
                                            strip_prefixes=self.mod_folder_strip_prefixes,
                                            per_mod_strip_prefixes=per_mod_strip,
                                            per_mod_deploy_dirs=per_mod_deploy,
                                            log_fn=_log,
                                            progress_fn=progress_fn,
                                            symlink_exts=_symlink_exts,
                                            exclude=custom_exclude or None,
                                            core_dir=data_dir.parent / (data_dir.name + "_Core"))
        _log(f"  Transferred {linked_mod} mod file(s).")

        _log("Step 3: Filling gaps with vanilla files from Data_Core/ ...")
        linked_core = deploy_core(data_dir, placed, mode=mode, log_fn=_log)
        _log(f"  Transferred {linked_core} vanilla file(s).")

        _log("Step 4: Symlinking plugins.txt into Proton prefix ...")
        self._symlink_plugins_txt(profile, _log)

        _log("Step 5: Applying archive invalidation ...")
        self.apply_archive_invalidation(_log)

        _log(
            f"Deploy complete. "
            f"{linked_mod} mod + {linked_core} vanilla "
            f"= {linked_mod + linked_core} total file(s) in Data/."
        )

    def restore(self, log_fn=None, progress_fn=None) -> None:
        """Restore Data/ to its vanilla state by moving Data_Core/ back."""
        _log = log_fn or (lambda _: None)

        if self._game_path is None:
            raise RuntimeError("Game path is not configured.")

        data_dir = self._game_path / "Data"

        _log("Restore: reverting archive invalidation ...")
        self.revert_archive_invalidation(_log)

        _profile_dir = self._active_profile_dir
        _entries = read_modlist(_profile_dir / "modlist.txt") if _profile_dir else []
        cleanup_custom_deploy_dirs(_profile_dir, _entries, log_fn=_log)

        custom_rules = self.custom_routing_rules
        if custom_rules and self._game_path:
            _log("Restore: removing custom-routed files ...")
            restore_custom_rules(
                self.get_effective_filemap_path(),
                self._game_path,
                rules=custom_rules,
                log_fn=_log,
            )

        _log("Restore: clearing Data/ and moving Data_Core/ back ...")
        restored = restore_data_core(
            data_dir,
            overwrite_dir=self.get_effective_overwrite_path(),
            staging_root=self.get_effective_mod_staging_path(),
            strip_prefixes=self.mod_folder_strip_prefixes,
            log_fn=_log,
        )
        _log(f"  Restored {restored} file(s). Data_Core/ removed.")

        self._remove_plugins_txt_symlink(_log)
        self._restore_launcher(_log)

        _log("Restore complete.")


class Fallout3_GOTY(Fallout_3):
    """Fallout 3 Game of the Year Edition — identical deployment to the base
    game, only the name, game_id, and steam_id differ."""

    @property
    def name(self) -> str:
        return "Fallout 3 GOTY"

    @property
    def game_id(self) -> str:
        return "Fallout3GOTY"

    @property
    def steam_id(self) -> str:
        return "22370"

    @property
    def nexus_game_domain(self) -> str:
        return "fallout3"

    @property
    def wizard_tools(self) -> list[WizardTool]:
        return self._base_wizard_tools() + [
            WizardTool(
                id="downgrade_fo3goty",
                label="Downgrade Fallout 3 GOTY",
                description=(
                    "Downgrade to pre-Anniversary Edition so that "
                    "the script extender (FOSE) works correctly."
                ),
                dialog_class_path="wizards.fallout_downgrade.FalloutDowngradeWizard",
            ),
            WizardTool(
                id="install_se_fo3goty",
                label="Install Script Extender (FOSE)",
                description="Download and install FOSE into the game folder.",
                dialog_class_path="wizards.script_extender.ScriptExtenderWizard",
                extra={
                    "download_url": "https://fose.silverlock.org/download/fose_v1_2_beta2.7z",
                    "archive_keywords": ["fose"],
                },
            ),
            WizardTool(
                id="run_wrye_bash_fo3goty",
                label="Run Wrye Bash",
                description="Download and run Wrye Bash.",
                dialog_class_path="wizards.wrye_bash.WryeBashWizard",
            ),
        ]


class Fallout_NV(Fallout_3):

    @property
    def wizard_tools(self) -> list[WizardTool]:
        return self._base_wizard_tools() + [
            WizardTool(
                id="install_se_fonv",
                label="Install Script Extender (xNVSE)",
                description="Download and install xNVSE into the game folder.",
                dialog_class_path="wizards.script_extender.ScriptExtenderWizard",
                extra={
                    "github_api_url": "https://api.github.com/repos/xNVSE/NVSE/releases/latest",
                    "archive_keywords": ["nvse"],
                },
            ),
            WizardTool(
                id="run_bethini_fonv",
                label="Run BethINI Pie",
                description="Install BethINI Pie and configure Fallout New Vegas INI settings.",
                dialog_class_path="wizards.bethini.BethINIWizard",
            ),
            WizardTool(
                id="run_wrye_bash_fonv",
                label="Run Wrye Bash",
                description="Download and run Wrye Bash.",
                dialog_class_path="wizards.wrye_bash.WryeBashWizard",
            ),
        ]

    @property
    def name(self) -> str:
        return "Fallout New Vegas"

    @property
    def game_id(self) -> str:
        return "FalloutNV"

    @property
    def exe_name(self) -> str:
        return "FalloutNVLauncher.exe"

    @property
    def steam_id(self) -> str:
        return "22380"

    @property
    def nexus_game_domain(self) -> str:
        return "newvegas"

    @property
    def loot_game_type(self) -> str:
        return "FalloutNV"
    
    @property
    def custom_routing_rules(self) -> list:
        from Utils.deploy import CustomRule
        return [
            CustomRule(dest="", filenames=["nvse_1_4.dll"]),
            CustomRule(dest="", filenames=["nvse_1_4.pdb"]),
            CustomRule(dest="", filenames=["nvse_editor_1_4.dll"]),
            CustomRule(dest="", filenames=["nvse_editor_1_4.pdb"]),
            CustomRule(dest="", filenames=["nvse_loader.exe"]),
            CustomRule(dest="", filenames=["nvse_loader.pdb"]),
            CustomRule(dest="", filenames=["nvse_steam_loader.dll"]),
            CustomRule(dest="", filenames=["nvse_steam_loader.pdb"]),
                ]

    @property
    def loot_masterlist_url(self) -> str:
        return "https://raw.githubusercontent.com/loot/falloutnv/v0.26/masterlist.yaml"

    _APPDATA_SUBPATH = Path("drive_c/users/steamuser/AppData/Local/FalloutNV")
    _MYGAMES_SUBPATH = Path("FalloutNV")
    _ARCHIVE_INI_FILENAME = "FALLOUT.ini"

    @property
    def _script_extender_exe(self) -> str:
        return "nvse_loader.exe"


class Fallout_4(Fallout_3):

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
                id="install_se_fo4",
                label="Install Script Extender (F4SE)",
                description="Download and install F4SE into the game folder.",
                dialog_class_path="wizards.script_extender.ScriptExtenderWizard",
                extra={
                    "download_url": "https://www.nexusmods.com/fallout4/mods/42147",
                    "archive_keywords": ["Fallout 4 Script Extender"],
                },
            ),
            WizardTool(
                id="run_bethini_fo4",
                label="Run BethINI Pie",
                description="Install BethINI Pie and configure Fallout 4 INI settings.",
                dialog_class_path="wizards.bethini.BethINIWizard",
            ),
            WizardTool(
                id="run_wrye_bash_fo4",
                label="Run Wrye Bash",
                description="Download and run Wrye Bash.",
                dialog_class_path="wizards.wrye_bash.WryeBashWizard",
            ),
        ]

    @property
    def name(self) -> str:
        return "Fallout 4"

    @property
    def game_id(self) -> str:
        return "Fallout4"

    @property
    def exe_name(self) -> str:
        return "Fallout4Launcher.exe"

    @property
    def steam_id(self) -> str:
        return "377160"

    @property
    def nexus_game_domain(self) -> str:
        return "fallout4"

    @property
    def loot_game_type(self) -> str:
        return "Fallout4"
    
    @property
    def custom_routing_rules(self) -> list:
        from Utils.deploy import CustomRule
        return [
            CustomRule(dest="", filenames=["f4se_loader.exe"]),
            CustomRule(dest="", filenames=["f4se_1_11_191.dll"]),
            CustomRule(dest="", filenames=["CustomControlMap.txt"]),
                ]

    @property
    def loot_masterlist_url(self) -> str:
        return "https://raw.githubusercontent.com/loot/fallout4/v0.21/masterlist.yaml"

    _APPDATA_SUBPATH = Path("drive_c/users/steamuser/AppData/Local/Fallout4")
    _MYGAMES_SUBPATH = Path("Fallout4")
    _ARCHIVE_INI_FILENAME = "Fallout4.ini"

    @property
    def _script_extender_exe(self) -> str:
        return "f4se_loader.exe"


class Fallout_4VR(Fallout_3):

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
                id="install_se_fo4vr",
                label="Install Script Extender (F4SEVR)",
                description="Download and install F4SEVR into the game folder.",
                dialog_class_path="wizards.script_extender.ScriptExtenderWizard",
                extra={
                    "download_url": "https://www.nexusmods.com/fallout4/mods/42159",
                    "archive_keywords": ["Fallout 4 Script Extender VR"],
                },
            ),
            WizardTool(
                id="run_wrye_bash_fo4vr",
                label="Run Wrye Bash",
                description="Download and run Wrye Bash.",
                dialog_class_path="wizards.wrye_bash.WryeBashWizard",
            ),
        ]

    @property
    def name(self) -> str:
        return "Fallout 4 VR"

    @property
    def game_id(self) -> str:
        return "Fallout4VR"

    @property 
    def exe_name(self) -> str:
        return "Fallout4VRLauncher.exe"

    @property
    def steam_id(self) -> str:
        return "611660"

    @property
    def nexus_game_domain(self) -> str:
        return "fallout4vr"
    
    @property
    def custom_routing_rules(self) -> list:
        from Utils.deploy import CustomRule
        return [
            CustomRule(dest="", filenames=["f4sevr_steam_loader.dll"]),
            CustomRule(dest="", filenames=["f4sevr_loader.exe"]),
            CustomRule(dest="", filenames=["f4sevr_1_2_72.dll"]),
                ]

    @property
    def loot_game_type(self) -> str:
        return "Fallout4VR"

    @property
    def loot_masterlist_url(self) -> str:
        return "https://raw.githubusercontent.com/loot/fallout4vr/v0.21/masterlist.yaml"

    _APPDATA_SUBPATH = Path("drive_c/users/steamuser/AppData/Local/Fallout4VR")
    _MYGAMES_SUBPATH = Path("Fallout4VR")
    _ARCHIVE_INI_FILENAME = "Fallout4.ini"

    @property
    def _script_extender_exe(self) -> str:
        return "f4sevr_loader.exe"


class Oblivion(Fallout_3):

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
    def loot_masterlist_url(self) -> str:
        return "https://raw.githubusercontent.com/loot/oblivion/refs/heads/v0.26/masterlist.yaml"

    @property
    def custom_routing_rules(self) -> list:
        from Utils.deploy import CustomRule
        return [
            CustomRule(dest="", filenames=["obse_loader.exe"]),
            CustomRule(dest="", filenames=["obse_1_2_416.dll"]),
            CustomRule(dest="", filenames=["obse_editor_1_2.dll"]),
            CustomRule(dest="", filenames=["obse_steam_loader.dll"]),
        ]

    _APPDATA_SUBPATH = Path("drive_c/users/steamuser/AppData/Local/Oblivion")
    archive_invalidation_enabled = False

    @property
    def _script_extender_exe(self) -> str:
        return "obse_loader.exe"

    def _plugins_txt_target(self) -> Path | None:
        """Return the in-prefix path where Oblivion expects Plugins.txt (capital P)."""
        if self._prefix_path is None:
            return None
        return self._prefix_path / self._APPDATA_SUBPATH / "Plugins.txt"

    def apply_archive_invalidation(self, log_fn) -> None:
        """Generate ArchiveInvalidation.txt listing all deployed .dds paths."""
        _log = log_fn
        if not self.archive_invalidation:
            return
        if self._game_path is None:
            _log("  WARN: Game path not set — skipping archive invalidation.")
            return
        filemap = self.get_effective_filemap_path()
        if not filemap.is_file():
            _log("  WARN: filemap.txt not found — skipping archive invalidation.")
            return

        dds_paths: list[str] = []
        for line in filemap.read_text(encoding="utf-8").splitlines():
            if not line or line.startswith("#"):
                continue
            rel_path = line.split("\t", 1)[0]
            if rel_path.lower().endswith(".dds"):
                dds_paths.append(rel_path.replace("\\", "/"))

        out = self._game_path / "ArchiveInvalidation.txt"
        out.write_text("\n".join(dds_paths) + "\n", encoding="utf-8")
        _log(f"  Wrote {len(dds_paths)} .dds path(s) to ArchiveInvalidation.txt.")

    def revert_archive_invalidation(self, log_fn) -> None:
        """Delete ArchiveInvalidation.txt."""
        if not self.archive_invalidation:
            return
        if self._game_path is None:
            return
        out = self._game_path / "ArchiveInvalidation.txt"
        if out.is_file():
            out.unlink()
            log_fn("  Removed ArchiveInvalidation.txt.")


class Skyrim(Fallout_3):

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

    @property
    def loot_game_type(self) -> str:
        return "Skyrim"

    @property
    def loot_masterlist_url(self) -> str:
        return "https://raw.githubusercontent.com/loot/skyrim/master/masterlist.yaml"

    @property
    def custom_routing_rules(self) -> list:
        from Utils.deploy import CustomRule
        return [
            CustomRule(dest="", filenames=["skse_loader.exe"]),
            CustomRule(dest="", filenames=["skse_1_9_32.dll"]),
            CustomRule(dest="", filenames=["skse_steam_loader.dll"]),
        ]

    _APPDATA_SUBPATH = Path("drive_c/users/steamuser/AppData/Local/Skyrim")
    _MYGAMES_SUBPATH = Path("Skyrim")
    _ARCHIVE_INI_FILENAME = "Skyrim.ini"

    @property
    def _script_extender_exe(self) -> str:
        return "skse_loader.exe"


class SkyrimVR(Fallout_3):

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
        ]

    @property
    def name(self) -> str:
        return "Skyrim VR"

    @property
    def game_id(self) -> str:
        return "skyrimvr"

    @property
    def exe_name(self) -> str:
        return "SkyrimVRLauncher.exe"

    @property
    def steam_id(self) -> str:
        return "611670"

    @property
    def nexus_game_domain(self) -> str:
        return "skyrimvr"

    @property
    def loot_game_type(self) -> str:
        return "SkyrimVR"

    @property
    def loot_masterlist_url(self) -> str:
        return "https://raw.githubusercontent.com/loot/skyrimvr/v0.21/masterlist.yaml"

    @property
    def custom_routing_rules(self) -> list:
        from Utils.deploy import CustomRule
        return [
            CustomRule(dest="", filenames=["sksevr_loader.exe"]),
            CustomRule(dest="", filenames=["sksevr_1_4_15.dll"]),
            CustomRule(dest="", filenames=["sksevr_steam_loader.dll"]),
        ]

    _APPDATA_SUBPATH = Path("drive_c/users/steamuser/AppData/Local/Skyrim VR")
    _MYGAMES_SUBPATH = Path("Skyrim VR")
    _ARCHIVE_INI_FILENAME = "Skyrim.ini"

    @property
    def _script_extender_exe(self) -> str:
        return "sksevr_loader.exe"


class Starfield(Fallout_3):

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
                id="install_se_starfield",
                label="Install Script Extender (SFSE)",
                description="Download and install SFSE into the game folder.",
                dialog_class_path="wizards.script_extender.ScriptExtenderWizard",
                extra={
                    "download_url": "https://www.nexusmods.com/starfield/mods/106",
                    "archive_keywords": ["sfse"],
                },
            ),
            WizardTool(
                id="run_bethini_starfield",
                label="Run BethINI Pie",
                description="Install BethINI Pie and configure Starfield INI settings.",
                dialog_class_path="wizards.bethini.BethINIWizard",
            ),
            WizardTool(
                id="run_wrye_bash_starfield",
                label="Run Wrye Bash",
                description="Download and run Wrye Bash.",
                dialog_class_path="wizards.wrye_bash.WryeBashWizard",
            ),
        ]

    @property
    def name(self) -> str:
        return "Starfield"

    @property
    def game_id(self) -> str:
        return "Starfield"

    @property
    def exe_name(self) -> str:
        # Starfield has no separate launcher; the main executable is the launch target.
        return "Starfield.exe"

    @property
    def steam_id(self) -> str:
        return "1716740"

    @property
    def nexus_game_domain(self) -> str:
        return "starfield"
    
    @property
    def loot_game_type(self) -> str:
        return "Starfield"

    @property
    def loot_masterlist_url(self) -> str:
        return "https://raw.githubusercontent.com/loot/starfield/v0.26/masterlist.yaml"

    @property
    def custom_routing_rules(self) -> list:
        from Utils.deploy import CustomRule
        return [
            CustomRule(dest="", filenames=["sfse_loader.exe"]),
            CustomRule(dest="", filenames=["sfse_1_15_222.dll"]),
        ]

    # plugins.txt lives at AppData/Local/Starfield/plugins.txt — same pattern as other Bethesda titles.
    _APPDATA_SUBPATH = Path("drive_c/users/steamuser/AppData/Local/Starfield")
    _MYGAMES_SUBPATH = Path("Starfield")
    _ARCHIVE_INI_FILENAME = "Starfield.ini"

    @property
    def _script_extender_exe(self) -> str:
        return "sfse_loader.exe"

class Enderal(Fallout_3):

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
    def loot_game_type(self) -> str:
        return "enderal"

    @property
    def loot_masterlist_url(self) -> str:
        return "https://raw.githubusercontent.com/loot/enderal/v0.26/masterlist.yaml"

    _APPDATA_SUBPATH = Path("drive_c/users/steamuser/AppData/Local/enderal")
    _MYGAMES_SUBPATH = Path("Enderal")
    _ARCHIVE_INI_FILENAME = "Skyrim.ini"

    @property
    def _script_extender_exe(self) -> str:
        return "skse_loader.exe"

    @property
    def wizard_tools(self) -> list[WizardTool]:
        return self._base_wizard_tools() + [
            WizardTool(
                id="run_wrye_bash_enderal",
                label="Run Wrye Bash",
                description="Download and run Wrye Bash.",
                dialog_class_path="wizards.wrye_bash.WryeBashWizard",
            ),
        ]

class EnderalSE(Fallout_3):

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
        return "enderal"

    @property
    def loot_masterlist_url(self) -> str:
        return "https://raw.githubusercontent.com/loot/enderal/v0.26/masterlist.yaml"

    _APPDATA_SUBPATH = Path("drive_c/users/steamuser/AppData/Local/Enderal Special Edition")
    _MYGAMES_SUBPATH = Path("Enderal Special Edition")
    _ARCHIVE_INI_FILENAME = "Skyrim.ini"

    @property
    def _script_extender_exe(self) -> str:
        return "skse64_loader.exe"

    @property
    def wizard_tools(self) -> list[WizardTool]:
        return self._base_wizard_tools() + [
            WizardTool(
                id="run_wrye_bash_enderalse",
                label="Run Wrye Bash",
                description="Download and run Wrye Bash.",
                dialog_class_path="wizards.wrye_bash.WryeBashWizard",
            ),
        ]