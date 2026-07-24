"""
fallout_nv.py
Fallout New Vegas game handler.
"""

from pathlib import Path

from Games.base_game import WizardTool
from Games.Bethesda.fallout_3 import Fallout_3
from Games.Bethesda.bethesda_ini import _set_ini_key


class Fallout_NV(Fallout_3):

    synthesis_registry_name = "FalloutNV"

    _archive_list_fix_name = "JIP LN NVSE"
    _archive_list_fix_path = "Data/NVSE/Plugins/jip_nvse.dll"

    vanilla_plugins = ["FalloutNV.esm"]
    vanilla_dlc_plugins = [
        "DeadMoney.esm", "HonestHearts.esm", "OldWorldBlues.esm",
        "LonesomeRoad.esm", "GunRunnersArsenal.esm",
        "CaravanPack.esm", "ClassicPack.esm",
        "MercenaryPack.esm", "TribalPack.esm", "FalloutNV_lang.esp",
    ]

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
                category="Setup and Installers",
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
                category="Setup and Installers",
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
    def steam_id(self) -> str:
        return "22380"

    @property
    def alt_steam_ids(self) -> list[str]:
        # 22490 is the Polish/Czech/Russian localized edition of FNV, which is
        # a separate Steam app sharing the same install/prefix layout. Owners of
        # that edition must launch through 22490, not 22380.
        return ["22490"]

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
            CustomRule(dest="", filenames=["nvse*.dll"], flatten=True, loose_only=True),
            CustomRule(dest="", folders=["Data"], flatten=True, loose_only=True),
            CustomRule(dest="", filenames=["nvse_loader.exe"], flatten=True, loose_only=True),
            CustomRule(dest="", filenames=["nvse*.pdb"], flatten=True, loose_only=True),
            CustomRule(dest="", filenames=["FNVpatch.exe"], flatten=True, loose_only=True),
            self._saves_routing_rule([".fos"]),
                ]

    @property
    def filemap_casing_pins(self) -> dict[str, str]:
        return {
            "nvse":"NVSE",
            "plugins":"Plugins",
        }

    @property
    def loot_masterlist_repo(self) -> str:
        return "falloutnv"

    _APPDATA_SUBPATH = Path("drive_c/users/steamuser/AppData/Local/FalloutNV")
    _APPDATA_SUBPATH_GOG = Path("drive_c/users/steamuser/AppData/Local/FalloutNV GOG")
    _MYGAMES_SUBPATH = Path("FalloutNV")
    _MYGAMES_SUBPATH_GOG = Path("FalloutNV GOG")
    _ARCHIVE_INI_FILENAME = "Fallout.ini"
    _ARCHIVE_PREFS_INI_FILENAME = "FalloutPrefs.ini"

    # MO2-style dummy-BSA invalidation (matches FalloutNVBSAInvalidation).
    _invalidation_bsa_name = "Fallout - Invalidation.bsa"
    _invalidation_bsa_version = 0x68

    @property
    def _script_extender_exe(self) -> str:
        return "nvse_loader.exe"

    # -----------------------------------------------------------------------
    # Automatic 4GB patch on deploy
    # -----------------------------------------------------------------------

    def _load_paths_extra(self, data: dict) -> None:
        super()._load_paths_extra(data)
        self._auto_4gb_patch = data.get("auto_4gb_patch", True)

    def _save_paths_extra(self) -> dict:
        out = super()._save_paths_extra()
        out["auto_4gb_patch"] = getattr(self, "_auto_4gb_patch", True)
        return out

    @property
    def auto_4gb_patch(self) -> bool:
        """Apply the 4GB patch automatically after each deploy (and revert it
        on restore). Default on; the Configure Game / Quick Configure option
        turns it off for users who manage the patch manually via the wizard."""
        return getattr(self, "_auto_4gb_patch", True)

    def set_auto_4gb_patch(self, value: bool) -> None:
        self._auto_4gb_patch = bool(value)
        self.save_paths()

    def _auto_4gb_active(self) -> bool:
        import os
        return self.auto_4gb_patch and os.environ.get("AMM_FNV_AUTO_4GB") != "0"

    def restore(self, log_fn=None, progress_fn=None) -> None:
        super().restore(log_fn=log_fn, progress_fn=progress_fn)
        self._undo_4gb_patch(log_fn)

    def _undo_4gb_patch(self, log_fn=None) -> None:
        """Put the vanilla exe back after a restore while automatic patching
        is on (post_deploy re-applies it on the next deploy). Only swaps a
        known patched exe with an existing backup; never fails the restore."""
        _log = log_fn or (lambda _m: None)
        if not self._auto_4gb_active():
            return
        game_root = self.get_game_path()
        if game_root is None or not game_root.is_dir():
            return
        try:
            from Utils.fnv4gb_tools import (
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
        from Utils.fnv4gb_tools import (
            BACKUP_NAME, EXE_NAME, apply_4gb_patch, inspect_exe,
        )
        info = inspect_exe(game_root)
        state = info["state"]
        if state == "patchable":
            variant = apply_4gb_patch(game_root)
            _log(f"4GB patch: auto-patched {EXE_NAME} ({variant} version) — "
                 f"original kept as {BACKUP_NAME}.")
        elif state == "unknown":
            _log(f"4GB patch: skipped — unrecognised {EXE_NAME} version "
                 f"(SHA-1 {info['hash']}). Verify game files, then use the "
                 f"4GB Patch wizard.")
        # "patched" / "missing" → nothing to do.

    # FalloutCustom.ini key/value set the TTW NVSE plugin expects (section, key,
    # value). Applied via _set_ini_key so existing keys are updated and missing
    # ones appended, leaving any other user keys untouched. Comments are omitted.
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
        """Set up per-profile INIs for TTW: enable profile-specific INIs, migrate
        the prefix INIs into the profile's 'ini files' folder (without
        overwriting), and write the TTW FalloutCustom.ini values. Idempotent.
        Caller must set the active-profile context to *profile* first."""
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
                src = mygames / name
                dst = ini_dir / name
                # Resolve through any symlink: a managed symlink already points
                # back into a profile, so there's nothing to migrate.
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
        custom_ini = ini_dir / self._TTW_CUSTOM_INI_FILENAME
        for section, key, value in self._TTW_CUSTOM_INI_VALUES:
            _set_ini_key(custom_ini, section, key, value)
        _log(f"  Wrote TTW values to '{custom_ini.name}'.")
