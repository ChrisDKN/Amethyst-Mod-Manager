"""
morrowind.py
Game handler for The Elder Scrolls III: Morrowind.

Key differences from other Bethesda games:
  - Mods install into <game_path>/Data Files/  (not Data/)
  - Does NOT use plugins.txt — load order is managed via Morrowind.ini
  - Plugin load order is determined by file mtime, not ini position
"""

from __future__ import annotations

from pathlib import Path

from Games.base_game import BaseGame, WizardTool
from Utils.deploy import (
    LinkMode,
    cleanup_custom_deploy_dirs,
    deploy_core,
    deploy_custom_rules,
    deploy_filemap,
    expand_separator_deploy_paths,
    expand_separator_link_modes,
    expand_separator_raw_deploy,
    load_per_mod_strip_prefixes,
    load_separator_deploy_paths,
    move_to_core,
    restore_custom_rules,
    restore_data_core,
)
from Utils.modlist import read_modlist
from Utils.config_paths import get_profiles_dir

_PROFILES_DIR = get_profiles_dir()


class Morrowind(BaseGame):

    # Morrowind can deploy by copying, so the saved "copy" mode must be honoured.
    deploy_mode_supports_copy = True

    vanilla_plugins = ["Morrowind.esm", "Tribunal.esm", "Bloodmoon.esm"]

    # Morrowind loads all .esm files before any .esp (TES3 has no header
    # master flag — the partition is purely by extension).
    plugins_master_block = True

    @property
    def supports_bain(self) -> bool:
        return True

    def __init__(self):
        self._game_path: Path | None = None
        self._prefix_path: Path | None = None
        self._deploy_mode: LinkMode = LinkMode.HARDLINK
        self._staging_path: Path | None = None
        self.load_paths()

    # -----------------------------------------------------------------------
    # Identity
    # -----------------------------------------------------------------------

    @property
    def name(self) -> str:
        return "Morrowind"

    @property
    def game_id(self) -> str:
        return "morrowind"

    @property
    def exe_name(self) -> str:
        return "Morrowind Launcher.exe"

    @property
    def plugin_extensions(self) -> list[str]:
        return [".esp", ".esm"]

    @property
    def steam_id(self) -> str:
        return "22320"

    @property
    def nexus_game_domain(self) -> str:
        return "morrowind"

    # Profile Groups: disabled for now — only Stardew Valley currently opts
    # in (see base_game.py's profile_groups_supported docstring) pending
    # wider review of the virtual-merge rework. Uncomment to enable once
    # this game's deploy path has been reviewed/tested.
    # @property
    # def profile_groups_supported(self) -> bool:
    #     return True

    @property
    def mod_folder_strip_prefixes(self) -> set[str]:
        return {"Data Files"}

    @property
    def mod_required_top_level_folders(self) -> set[str]:
        return {
            "bookart",
            "distantlod",
            "fonts",
            "icons",
            "iwy",
            "kw",
            "meshes",
            "music",
            "mwse",
            "shaders",
            "sound",
            "splash",
            "textures",
            "video",
        }

    @property
    def mod_auto_strip_until_required(self) -> bool:
        return True

    @property
    def mod_required_file_types(self) -> set[str]:
        return {".esp", ".esm", ".ini"}

    @property
    def mod_install_as_is_if_no_match(self) -> bool:
        return True

    @property
    def conflict_ignore_filenames(self) -> set[str]:
        return {"info.xml", "*read*.txt"}

    @property
    def loot_sort_enabled(self) -> bool:
        return True

    @property
    def loot_game_type(self) -> str:
        return "Morrowind"

    @property
    def loot_masterlist_repo(self) -> str:
        return "morrowind"

    @property
    def wine_dll_overrides(self) -> dict[str, str]:
        return {"d3d8": "native,builtin", "dinput8": "native,builtin"}

    @property
    def frameworks(self) -> dict[str, str]:
        return {"MGE XE": "MGEXEgui.exe"}
    
    @property
    def custom_routing_rules(self) -> list:
        from Utils.deploy import CustomRule
        return [
            CustomRule(dest="", filenames=["SlimDX.dll"], flatten=True, loose_only=True),
            CustomRule(dest="", filenames=["Newtonsoft.Json.dll"], flatten=True, loose_only=True),
            CustomRule(dest="", filenames=["MWSE-Update.exe"], flatten=True, loose_only=True),
            CustomRule(dest="", filenames=["MGEXEgui.exe"], flatten=True, loose_only=True),
            CustomRule(dest="", filenames=["dinput8.dll"], flatten=True, loose_only=True),
            CustomRule(dest="", filenames=["d3d8.dll"], flatten=True, loose_only=True),
            CustomRule(dest="", folders=["mge3"], flatten=True, loose_only=True),
        ]

    @property
    def wizard_tools(self) -> list[WizardTool]:
        return self._base_wizard_tools() + [
            WizardTool(
                id="install_mgexe",
                label="Install MGE XE",
                description=(
                    "Download and install MGE XE (Morrowind Graphics Extender), "
                    "which includes MWSE."
                ),
                dialog_class_path="Games.Morrowind.mgexe_wizard.MGEXEWizard",
            ),
            WizardTool(
                id="install_mcp",
                label="Install Morrowind Code Patch",
                description=(
                    "Download and run the Morrowind Code Patch to apply "
                    "engine-level bug fixes and improvements."
                ),
                dialog_class_path="Games.Morrowind.mcp_wizard.MCPWizard",
            ),
            WizardTool(
                id="run_wrye_bash_morrowind",
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
        if self._game_path is None:
            return None
        return self._game_path / "Data Files"

    def runtime_snapshot_exclude_dirs(self) -> set[str] | None:
        # 'Data Files/' is reverted via its _Core backup; capture only files outside it.
        return {"Data Files"}

    def get_mod_staging_path(self) -> Path:
        if self._staging_path is not None:
            return self._staging_path / "mods"
        return _PROFILES_DIR / self.name / "mods"

    # -----------------------------------------------------------------------
    # Configuration persistence
    # -----------------------------------------------------------------------

    # load_paths / save_paths are inherited from BaseGame (profile-aware);
    # deploy_mode_supports_copy preserves the "copy" deploy mode.

    def set_staging_path(self, path: Path | str | None) -> None:
        self._staging_path = Path(path) if path else None
        self.save_paths()

    def get_prefix_path(self) -> Path | None:
        return self._prefix_path

    def set_prefix_path(self, path: Path | str | None) -> None:
        self._prefix_path = Path(path) if path else None
        self.save_paths()

    def get_deploy_mode(self) -> LinkMode:
        return self._deploy_mode

    def set_deploy_mode(self, mode: LinkMode) -> None:
        self._deploy_mode = mode
        self.save_paths()

    # -----------------------------------------------------------------------
    # Deployment
    # -----------------------------------------------------------------------

    def deploy(self, log_fn=None, mode: LinkMode = LinkMode.HARDLINK,
               profile: str = "default", progress_fn=None) -> None:
        _log = log_fn or (lambda _: None)

        if self._game_path is None:
            raise RuntimeError("Game path is not configured.")

        data_dir = self._game_path / "Data Files"
        filemap  = self.get_effective_filemap_path()
        staging  = self.get_effective_mod_staging_path()

        if not data_dir.is_dir():
            raise RuntimeError(f"'Data Files' directory not found: {data_dir}")
        if not filemap.is_file():
            raise RuntimeError(
                f"filemap.txt not found: {filemap}\n"
                "Run 'Build Filemap' before deploying."
            )

        _log("Step 1: Moving 'Data Files/' → 'Data Files_Core/' ...")
        move_to_core(data_dir, log_fn=_log)
        _log("  Backed up existing files → 'Data Files_Core/'.")

        profile_dir    = self.get_profile_root() / "profiles" / profile
        per_mod_strip  = load_per_mod_strip_prefixes(profile_dir)
        _sep_deploy    = load_separator_deploy_paths(profile_dir)
        _sep_entries   = read_modlist(profile_dir / "modlist.txt") if _sep_deploy else []
        per_mod_deploy = expand_separator_deploy_paths(_sep_deploy, _sep_entries) or None
        per_mod_modes  = expand_separator_link_modes(_sep_deploy, _sep_entries) or None
        per_mod_raw    = expand_separator_raw_deploy(_sep_deploy, _sep_entries) or None

        # Custom-routed files (MGE XE loose files: d3d8.dll, MGEXEgui.exe,
        # mge3/, …) are placed under the game root — NOT 'Data Files/' — so run
        # this before the normal deploy and exclude the handled paths from it.
        custom_rules = self.custom_routing_rules
        custom_exclude: set[str] = set()
        if custom_rules:
            _log("Step 1b: Routing files via custom rules ...")
            custom_exclude = deploy_custom_rules(
                filemap, self._game_path, staging,
                rules=custom_rules,
                mode=mode,
                strip_prefixes=self.mod_folder_strip_prefixes,
                per_mod_strip_prefixes=per_mod_strip,
                per_mod_link_modes=per_mod_modes,
                log_fn=_log,
                progress_fn=progress_fn,
                prefix_root=self.get_prefix_path(),
                raw_mods=per_mod_raw,
            )

        _log(f"Step 2: Transferring mod files into 'Data Files/' ({mode.name}) ...")
        linked_mod, placed = deploy_filemap(
            filemap, data_dir, staging,
            mode=mode,
            strip_prefixes=self.mod_folder_strip_prefixes,
            per_mod_strip_prefixes=per_mod_strip,
            per_mod_deploy_dirs=per_mod_deploy,
            per_mod_link_modes=per_mod_modes,
            log_fn=_log,
            progress_fn=progress_fn,
            exclude=custom_exclude or None,
            core_dir=data_dir.parent / (data_dir.name + "_Core"),
        )
        _log(f"  Transferred {linked_mod} mod file(s).")

        _log("Step 3: Filling gaps with vanilla files from 'Data Files_Core/' ...")
        linked_core = deploy_core(data_dir, placed, mode=mode, log_fn=_log)
        _log(f"  Transferred {linked_core} vanilla file(s).")

        _log("Step 4: Normalising plugin extensions to lowercase in 'Data Files/' ...")
        _renamed = 0
        for _f in data_dir.iterdir():
            if _f.is_file():
                _dot = _f.name.rfind(".")
                if _dot != -1:
                    _ext = _f.name[_dot:]
                    if _ext != _ext.lower():
                        _f.rename(_f.parent / (_f.name[:_dot] + _ext.lower()))
                        _renamed += 1
        _log(f"  Renamed {_renamed} file(s).")

        _log("Step 5: Updating Morrowind.ini [Game Files] and setting plugin mtimes ...")
        from Games.Morrowind.morrowind_ini import update_morrowind_ini
        plugins_txt = profile_dir / "plugins.txt"
        update_morrowind_ini(
            ini_path=self._game_path / "Morrowind.ini",
            plugins_txt=plugins_txt,
            data_files_dir=data_dir,
            staging_root=staging,
            log_fn=_log,
        )

        _log(
            f"Deploy complete. "
            f"{linked_mod} mod + {linked_core} vanilla "
            f"= {linked_mod + linked_core} total file(s) in 'Data Files/'."
        )

        # Capture runtime files generated outside 'Data Files/' on the next restore.
        self.snapshot_root_for_runtime_capture(log_fn=_log)

    def restore(self, log_fn=None, progress_fn=None) -> None:
        _log = log_fn or (lambda _: None)

        if self._game_path is None:
            raise RuntimeError("Game path is not configured.")

        data_dir = self._game_path / "Data Files"

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
                prefix_root=self.get_prefix_path(),
            )

        _log("Restore: removing mod plugins from Morrowind.ini ...")
        from Games.Morrowind.morrowind_ini import restore_morrowind_ini
        if self._game_path:
            restore_morrowind_ini(self._game_path / "Morrowind.ini", data_dir, log_fn=_log)

        _log("Restore: clearing 'Data Files/' and moving 'Data Files_Core/' back ...")
        try:
            restored = restore_data_core(
                data_dir,
                overwrite_dir=self.get_effective_overwrite_path(),
                staging_root=self.get_effective_mod_staging_path(),
                strip_prefixes=self.mod_folder_strip_prefixes,
                log_fn=_log,
            )
            _log(f"  Restored {restored} file(s). 'Data Files_Core/' removed.")
        except RuntimeError as e:
            _log(f"  Skipping data restore: {e}")

        moved = self.capture_runtime_files_to_root_folder(log_fn=_log)
        if moved:
            _log(f"  Moved {moved} runtime file(s) to Root_Folder/.")

        _log("Restore complete.")
