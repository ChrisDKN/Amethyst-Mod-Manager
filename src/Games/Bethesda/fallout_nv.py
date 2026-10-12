from pathlib import Path

from Games.base_game import WizardTool
from Games.Bethesda.bethesda_base import BethesdaBase
from Games.Bethesda.bethesda_ini import _set_ini_key


class Fallout_NV(BethesdaBase):
    _extra_auto_install_deps = ("lavfilters",)
    _archive_list_needs_mod_bsas = True

    synthesis_registry_name = "FalloutNV"

    _archive_list_fix_name = "JIP LN NVSE"
    _archive_list_fix_path = "Data/NVSE/Plugins/jip_nvse.dll"
    direct_play_requires_direct = True

    # Keep masters and DLC first; leave FalloutNV_lang.esp in the saved order.
    primary_plugin_order = [
        "FalloutNV.esm",
        "DeadMoney.esm", "HonestHearts.esm", "OldWorldBlues.esm",
        "LonesomeRoad.esm", "GunRunnersArsenal.esm",
        "ClassicPack.esm", "MercenaryPack.esm", "TribalPack.esm",
        "CaravanPack.esm",
    ]
    vanilla_plugins = ["FalloutNV.esm"]
    vanilla_dlc_plugins = primary_plugin_order[1:] + ["FalloutNV_lang.esp"]

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
                id="fnv_4gb_patch",
                label="Apply 4GB Patch",
                description="Patch FalloutNV.exe to use 4 GB of memory (keeps a backup that can be restored).",
                dialog_class_path="wizards.fnv_4gb_patch.Fnv4GbPatchWizard",
            ),
            WizardTool(
                id="install_ttw",
                label="Install Tale of Two Wastelands",
                description="Run the native Linux TTW installer (merges Fallout 3 + New Vegas) and add the result as a mod. Requires Fallout 3 installed and a TTW .mpi package from mod.pub.",
                dialog_class_path="wizards.ttw.TTWInstallerWizard",
                category="Setup and Installers",
            ),
            WizardTool(
                id="fnv_bsa_decompressor",
                label="BSA Decompressor",
                description="Decompress the vanilla BSA archives for faster loading (native Linux MPI installer) and add the result as a mod. Needs the FNV BSA Decompressor download from Nexus.",
                dialog_class_path="wizards.bsa_decompressor.BSADecompressorWizard",
                category="Setup and Installers",
            ),
            WizardTool(
                id="fnv_esm_fixes",
                label="Install Ultimate Edition ESM Fixes",
                description="Patch the vanilla .esm masters with community bugfixes (native Linux MPI installer) and add the result as a mod. Needs the Ultimate Edition ESM Fixes Remastered download from Nexus.",
                dialog_class_path="wizards.esm_fixes.ESMFixesWizard",
                category="Setup and Installers",
            ),
            WizardTool(
                id="fnv_viva_new_vegas",
                label="Install Viva New Vegas",
                description="Download the curated Viva New Vegas modlist profile and install it",
                dialog_class_path="wizards.curated_profile.CuratedProfileWizard",
                category="Install Modlist",
                extra={
                    "profile_repo_path": "Profiles/FalloutNV/Viva_New_Vegas.amethyst",
                    "display_name": "Viva New Vegas",
                    "esm_fixes_step": True,
                    "bsa_decompressor_step": True,
                    "fnv_4gb_step": True,
                    "info_url": "https://vivanewvegas.moddinglinked.com/",
                },
            ),
            WizardTool(
                id="fnv_viva_new_vegas_extended",
                label="Install Viva New Vegas Extended",
                description="Download the curated Viva New Vegas Extended modlist profile and install it",
                dialog_class_path="wizards.curated_profile.CuratedProfileWizard",
                category="Install Modlist",
                extra={
                    "profile_repo_path": "Profiles/FalloutNV/Viva_New_Vegas_Extended.amethyst",
                    "display_name": "Viva New Vegas Extended",
                    "esm_fixes_step": True,
                    "bsa_decompressor_step": True,
                    "fnv_4gb_step": True,
                    "info_url": "https://vivanewvegas.moddinglinked.com/",
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
            self._xlodgen_wizard_tool("fonv"),
            *self._xedit_wizard_tools(
                build="FNVEdit", id_suffix="fonv",
                nexus_url="https://www.nexusmods.com/newvegas/mods/34703?tab=files",
                nexus_file_id=1000128948,
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
    def direct_launch_exes(self) -> list[str]:
        return ["FalloutNV.exe"]

    @property
    def steam_id(self) -> str:
        return "22380"

    @property
    def alt_steam_ids(self) -> list[str]:
        # The localized Steam edition uses app 22490 with the same install layout.
        return ["22490"]

    @property
    def nexus_game_domain(self) -> str:
        return "newvegas"

    @property
    def loot_game_type(self) -> str:
        return "FalloutNV"

    @property
    def custom_routing_rules(self) -> list:
        from Utils.deployment import CustomRule
        return [
            CustomRule(
                rule_id='fallout_nv:fde4d860f961', dest='', filenames=['nvse*.dll'], flatten=True,
                loose_only=True,
            ),
            CustomRule(
                rule_id='fallout_nv:42b2892ccb1f', dest='', folders=['Data'], flatten=True,
                loose_only=True,
            ),
            CustomRule(
                rule_id='fallout_nv:4b0617be17c4', dest='', filenames=['nvse_loader.exe'],
                flatten=True, loose_only=True,
            ),
            CustomRule(
                rule_id='fallout_nv:2c5a2dd0484d', dest='', filenames=['nvse*.pdb'], flatten=True,
                loose_only=True,
            ),
            CustomRule(
                rule_id='fallout_nv:a280a808ee8e', dest='', filenames=['FNVpatch.exe'],
                flatten=True, loose_only=True,
            ),
            self._saves_routing_rule([".fos"]),
        ]

    @property
    def filemap_casing_pins(self) -> dict[str, str]:
        return {
            "nvse":"NVSE",
            "plugins":"Plugins",
        }

    @property
    def restore_whitelist(self) -> list:
        # Keep the 4GB backup out of runtime capture when automatic undo is disabled.
        from Utils.deployment import RestoreWhitelistRule
        from Utils.bethesda.fnv4gb import BACKUP_NAME
        return super().restore_whitelist + [
            RestoreWhitelistRule(path="", filenames=[BACKUP_NAME]),
        ]

    @property
    def loot_masterlist_repo(self) -> str:
        return "falloutnv"

    _APPDATA_SUBPATH = Path("drive_c/users/steamuser/AppData/Local/FalloutNV")
    _APPDATA_SUBPATH_GOG = Path("drive_c/users/steamuser/AppData/Local/FalloutNV GOG")
    _MYGAMES_SUBPATH = Path("FalloutNV")
    _MYGAMES_SUBPATH_GOG = Path("FalloutNV GOG")
    _CUSTOM_INI_FILENAME = "FalloutCustom.ini"
    _ARCHIVE_INI_FILENAME = "Fallout.ini"
    _ARCHIVE_PREFS_INI_FILENAME = "FalloutPrefs.ini"
    _PROFILE_SAVES_INI_FILES = ("Fallout.ini", "FalloutCustom.ini")

    # MO2-style dummy-BSA invalidation (matches FalloutNVBSAInvalidation).
    _invalidation_bsa_name = "Fallout - Invalidation.bsa"
    _invalidation_bsa_version = 0x68

    @property
    def _script_extender_exe(self) -> str:
        return "nvse_loader.exe"

    def _is_4gb_patched_exe(self, exe: Path) -> bool:
        if exe.name.casefold() != "falloutnv.exe":
            return False
        try:
            from Utils.bethesda.fnv4gb import inspect_exe
            return inspect_exe(exe.parent)["state"] == "patched"
        except (OSError, RuntimeError):
            return False

    @property
    def direct_play_exe(self) -> str:
        game_root = self.get_game_path()
        if (game_root is not None
                and self._is_4gb_patched_exe(game_root / "FalloutNV.exe")):
            return "FalloutNV.exe"
        return ""

    def _vfs_direct_launch_exe(self) -> str:
        try:
            from Utils.vfs import virtual_file_path
            exe = virtual_file_path(self, "FalloutNV.exe")
        except (OSError, RuntimeError):
            return ""
        return "FalloutNV.exe" if (
            exe is not None and self._is_4gb_patched_exe(exe)) else ""

    def prepare_launch_environment_for_exe(
            self, exe_path: Path, env: dict[str, str], log_fn=None) -> None:
        app_ids = {env.get("STEAM_COMPAT_APP_ID"), env.get("SteamAppId")}
        if ("22380" not in app_ids
                or not self._is_4gb_patched_exe(Path(exe_path))):
            return
        env["PROTONFIXES_DISABLE"] = "1"
        if log_fn is not None:
            log_fn("Run EXE: disabled ProtonFixes for the 4GB-patched "
                   "FalloutNV.exe launch.")

    def swap_launcher(self, log_fn) -> None:
        if self._game_path is not None:
            try:
                from Utils.bethesda.fnv4gb import inspect_exe
                state = inspect_exe(self._game_path)["state"]
            except (OSError, RuntimeError):
                state = "unknown"
            if state == "patched":
                self._restore_launcher(log_fn)
                log_fn("  Launcher swap skipped - the 4GB-patched FalloutNV.exe "
                       "loads xNVSE directly.")
                return
        super().swap_launcher(log_fn)

    def _load_paths_extra(self, data: dict) -> None:
        super()._load_paths_extra(data)
        self._auto_4gb_patch = data.get("auto_4gb_patch", True)

    def _save_paths_extra(self) -> dict:
        out = super()._save_paths_extra()
        out["auto_4gb_patch"] = getattr(self, "_auto_4gb_patch", True)
        return out

    @property
    def auto_4gb_patch(self) -> bool:
        return getattr(self, "_auto_4gb_patch", True)

    def set_auto_4gb_patch(self, value: bool) -> None:
        self._auto_4gb_patch = bool(value)
        self.save_paths()

    def _auto_4gb_active(self) -> bool:
        import os
        return self.auto_4gb_patch and os.environ.get("AMM_FNV_AUTO_4GB") != "0"

    def restore(self, log_fn=None, progress_fn=None) -> None:
        self._undo_4gb_patch(log_fn)
        super().restore(log_fn=log_fn, progress_fn=progress_fn)

    def _undo_4gb_patch(self, log_fn=None) -> None:
        _log = log_fn or (lambda _m: None)
        if not self._auto_4gb_active():
            return
        if self.vfs_launch_enabled:
            # The patch exists only in the VFS root layer; the real executable is unchanged.
            return
        game_root = self.get_game_path()
        if game_root is None or not game_root.is_dir():
            return
        try:
            from Utils.bethesda.fnv4gb import (
                EXE_NAME, inspect_exe, restore_backup,
            )
            info = inspect_exe(game_root)
            if info["state"] == "patched" and info["backup_exists"]:
                restore_backup(game_root)
                _log(f"4GB patch: restored original {EXE_NAME} from backup.")
        except Exception as exc:
            _log(f"WARN: 4GB patch revert failed: {exc}")

    def post_deploy(self, log_fn=None) -> None:
        _log = log_fn or (lambda _m: None)
        if not self._auto_4gb_active():
            return
        game_root = self.get_game_path()
        if game_root is None or not game_root.is_dir():
            return
        from Utils.bethesda.fnv4gb import (
            BACKUP_NAME, EXE_NAME, apply_4gb_patch, inspect_exe,
        )
        patch_root = game_root
        if self.vfs_launch_enabled:
            from Utils.vfs import virtual_root_write_path
            virtual_exe = virtual_root_write_path(self, EXE_NAME)
            virtual_exe.parent.mkdir(parents=True, exist_ok=True)
            if not virtual_exe.is_file():
                import shutil
                shutil.copy2(game_root / EXE_NAME, virtual_exe)
            patch_root = virtual_exe.parent
        info = inspect_exe(patch_root)
        state = info["state"]
        if state == "patchable":
            variant = apply_4gb_patch(patch_root)
            state = "patched"
            if self.vfs_launch_enabled:
                (patch_root / BACKUP_NAME).unlink(missing_ok=True)
                _log(f"4GB patch: added a virtual patched {EXE_NAME} "
                     f"({variant} version); the game folder is unchanged.")
            else:
                _log(f"4GB patch: auto-patched {EXE_NAME} ({variant} version) - "
                     f"original kept as {BACKUP_NAME}.")
        elif state == "unknown":
            _log(f"4GB patch: skipped - unrecognised {EXE_NAME} version "
                 f"(SHA-1 {info['hash']}). Verify game files, then use the "
                 f"4GB Patch wizard.")
        if state == "patched" and not self.vfs_launch_enabled:
            self._restore_launcher(_log)
        # "patched" / "missing" → nothing to do.

    _TTW_CUSTOM_INI_VALUES: list[tuple[str, str, str]] = [
        ("Audio", "bMultiThreadAudio", "1"),
        ("Audio", "bUseAudioDebugInformation", "0"),
        ("Audio", "iAudioCacheSize", "16384"),
        ("Audio", "iMaxSizeForCachedSound", "2048"),
        ("BackgroundLoad", "bSelectivePurgeUnusedOnFastTravel", "1"),
        ("BackgroundLoad", "bBackgroundLoadLipFiles", "1"),
        ("Controls", "fForegroundMouseAccelBase", "0"),
        ("Controls", "fForegroundMouseAccelTop", "0"),
        ("Controls", "fForegroundMouseBase", "0"),
        ("Controls", "fForegroundMouseMult", "0"),
        ("Display", "bFull Screen", "1"),
        ("Display", "iPresentInterval", "1"),
        ("Display", "iTexMipMapSkip", "0"),
        ("Display", "bDrawShadows", "0"),
        ("Display", "iActorShadowCountInt", "0"),
        ("Display", "iActorShadowCountExt", "0"),
        ("Display", "fDefaultWorldFOV", "75.0000"),
        ("Display", "fDefault1stPersonFOV", "55.0000"),
        ("Display", "fPipboy1stPersonFOV", "47.0"),
        ("General", "bPreemptivelyUnloadCells", "1"),
        ("General", "iNumHWThreads", "3"),
        ("General", "SCharGenQuest", "001FFFF8"),
        ("General", "SIntroMovie", ""),
        ("Grass", "fGrassStartFadeDistance", "11200"),
        ("Grass", "b30GrassVS", "1"),
        ("Water", "bForceHighDetailReflections", "0"),
        ("BlurShaderHDR", "bDoHighDynamicRange", "1"),
        ("BlurShader", "bUseBlurShader", "0"),
        ("PipBoy", "fLightEffectFadeDuration", "400"),
    ]

    _TTW_CUSTOM_INI_FILENAME = "FalloutCustom.ini"
    # INIs migrated from the prefix into the profile's "ini files" folder.
    _TTW_MIGRATE_INI_NAMES = ("Fallout.ini", "FalloutPrefs.ini", "FalloutCustom.ini")

    def setup_ttw_custom_ini(self, profile: str, log_fn=None) -> None:
        import shutil

        _log = log_fn or (lambda _m: None)

        # 1. Enable profile-specific INIs for this profile.
        if not self._profile_ini_files:
            self.set_profile_ini_files(True)
            _log(f"  Enabled profile-specific INI files for '{profile}'.")

        ini_dir = self._profile_ini_dir(profile)
        ini_dir.mkdir(parents=True, exist_ok=True)

        # 2. Migrate INIs from the prefix → profile, without overwriting.
        for mygames in self._mygames_paths():
            for name in self._TTW_MIGRATE_INI_NAMES:
                src = self._resolve_ini_path(mygames, name)
                dst = self._resolve_ini_path(ini_dir, name)
                # A managed symlink already points to the profile; skip migration.
                if not src.exists() or src.is_symlink():
                    continue
                if dst.exists():
                    _log(f"  Kept existing '{name}' in profile (not overwritten).")
                    continue
                try:
                    shutil.copy2(src, dst)
                    _log(f"  Migrated '{name}' from prefix → profile 'ini files'.")
                except OSError as exc:
                    _log(f"  WARN: could not migrate '{name}': {exc}")

        # 3. Create / update FalloutCustom.ini with the TTW values.
        custom_ini = self._resolve_ini_path(ini_dir, self._TTW_CUSTOM_INI_FILENAME)
        for section, key, value in self._TTW_CUSTOM_INI_VALUES:
            _set_ini_key(custom_ini, section, key, value)
        _log(f"  Wrote TTW values to '{custom_ini.name}'.")


class Fallout_NC(Fallout_NV):
    _extra_auto_install_deps = ()
    supports_script_extender_swap = False
    _new_california_plugins = [
        "NewCalifornia.esm",
        "NewCalifornia DLC Control.esp",
        "NewCalifornia Courier Stash Control.esp",
    ]
    primary_plugin_order = [
        *Fallout_NV.primary_plugin_order,
        "NewCalifornia.esm",
        "FalloutNV_lang.esp",
        "NewCalifornia DLC Control.esp",
        "NewCalifornia Courier Stash Control.esp",
    ]
    vanilla_plugins = [
        *Fallout_NV.vanilla_plugins,
        *_new_california_plugins,
    ]
    _ARCHIVE_INI_FILENAME = "Fallout.ini"

    @property
    def name(self) -> str:
        return "Fallout New California (GOG)"

    @property
    def game_id(self) -> str:
        return "FalloutNC"

    @property
    def exe_name(self) -> str:
        return "FalloutNV.exe"

    @property
    def steam_id(self) -> str:
        return ""

    @property
    def alt_steam_ids(self) -> list[str]:
        return []

    def _remove_plugins_txt_symlink(self, log_fn) -> None:
        from Utils.plugins import deploy_plugins_copy

        data_dir = self.get_vanilla_plugins_path()
        try:
            present = {
                entry.name.casefold(): entry.name
                for entry in data_dir.iterdir() if entry.is_file()
            } if data_dir is not None else {}
        except OSError:
            present = {}
        baseline = [
            present[name.casefold()] for name in self.primary_plugin_order
            if name.casefold() in present
        ]
        if not baseline:
            super()._remove_plugins_txt_symlink(log_fn)
            return

        content = "\n".join(baseline) + "\n"
        for target in self._plugins_txt_targets():
            deploy_plugins_copy(target.parent, target.name, content, log_fn)
        log_fn("  Restored the Fallout New California base plugins list.")

    @property
    def wizard_tools(self) -> list[WizardTool]:
        return self._base_wizard_tools() + [
            WizardTool(
                id="fnv_4gb_patch",
                label="Apply 4GB Patch",
                description="Patch FalloutNV.exe to use 4 GB of memory (keeps a backup that can be restored).",
                dialog_class_path="wizards.fnv_4gb_patch.Fnv4GbPatchWizard",
            ),
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
            self._xlodgen_wizard_tool("falloutnc"),
            *self._xedit_wizard_tools(
                build="FNVEdit", id_suffix="fonv",
                nexus_url="https://www.nexusmods.com/newvegas/mods/34703?tab=files",
                nexus_file_id=1000128948,
            ),
        ]
