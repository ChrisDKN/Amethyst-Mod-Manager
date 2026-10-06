"""
skyrim_vr.py
Skyrim VR game handler.
"""

import filecmp
import os
import shutil
import tempfile
from pathlib import Path

from Games.base_game import WizardTool
from Games.Bethesda.bethesda_ini import _read_ini_key, _set_ini_key
from Games.Bethesda.fallout_3 import Fallout_3
from Utils.atomic_write import write_atomic
from Utils.games.frameworks import resolve_file_ci


class SkyrimVR(Fallout_3):

    supports_script_extender_swap = False
    post_deploy_failure_is_fatal = True
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
                id="run_pandora_skyrimvr",
                label="Run Pandora",
                description="Install or run Pandora Behaviour Engine+.",
                dialog_class_path="wizards.pandora.PandoraWizard",
            ),
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
            self._xlodgen_wizard_tool("skyrimvr"),
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
        from Utils.deployment import CustomRule
        return [
            CustomRule(rule_id='skyrim_vr:ba8a5a10a017', dest="", filenames=["sksevr_loader.exe"], flatten=True, loose_only=True),
            CustomRule(rule_id='skyrim_vr:94020df19209', dest="", filenames=["sksevr*.dll"], flatten=True, loose_only=True),
            CustomRule(rule_id='skyrim_vr:42b2892ccb1f', dest="", folders=["Data"], flatten=True, loose_only=True),
            CustomRule(rule_id='skyrim_vr:d77e45d2c1b6', dest="", folders=["bindings"], flatten=True, loose_only=True),
            CustomRule(rule_id='skyrim_vr:7bcf1a7ed4b0', dest="", folders=["src"], flatten=True, loose_only=True),
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

    @property
    def _script_extender_runtime_ini(self) -> Path:
        return Path("Data/SKSE/skse.ini")

    def _vfs_direct_launch_exe(self) -> str:
        return self._script_extender_exe

    @property
    def restore_whitelist(self) -> list:
        from Utils.deployment import RestoreWhitelistRule
        return [
            *super().restore_whitelist,
            RestoreWhitelistRule(path="", filenames=["SkyrimVR.bak"]),
        ]

    def _script_extender_paths(self) -> tuple[Path, Path, Path] | None:
        game_path = self.get_game_path()
        if game_path is None:
            return None
        names = (self._script_extender_exe, self.exe_name, "SkyrimVR.bak")
        return tuple(resolve_file_ci(game_path, Path(name)) or game_path / name
                     for name in names)

    def get_launch_handoff(self, profile: str | None = None):
        if self.vfs_launch_enabled:
            return super().get_launch_handoff(profile)
        paths = self._script_extender_paths()
        if paths is None or not paths[0].is_file():
            return None
        from Utils.launchers.handoff import LaunchHandoff, LaunchHandoffField
        loader, runtime, _backup = paths
        command = (
            "bash -c 'exec \"${@/%"
            f"{runtime.name}/{loader.name}"
            "}\"' -- %command%"
        )
        return LaunchHandoff(
            launcher_id="steam-sksevr",
            launcher_name="Steam",
            instructions=(
                "Open Properties → General and paste this into Launch Options."
            ),
            fields=(LaunchHandoffField("Launch Options", command),),
            note=(
                "Set this once to make Steam launch SKSEVR. "
                "Keep the game executable named SkyrimVR.exe. "
                "Amethyst's sksevr_loader.exe Run entry launches it directly "
                "and does not require this setting."
            ),
        )

    def _remove_script_extender_runtime_override(self, log_fn) -> None:
        # Recover before Restore removes the deployed INI and loader.
        self._restore_launcher(log_fn)

    def _restore_launcher(self, log_fn) -> None:
        paths = self._script_extender_paths()
        if paths is None:
            return
        loader, runtime, backup = paths
        if not backup.is_file():
            return
        ini_path = self._script_extender_runtime_ini_path()
        override_matches = bool(ini_path and (
            _read_ini_key(ini_path, "Loader", "RuntimeName",
                          case_insensitive=True) or ""
        ).casefold() == backup.name.casefold())
        runtime_matches = (
            runtime.is_file() and loader.is_file()
            and filecmp.cmp(runtime, loader, shallow=False)
        )
        if not (override_matches or runtime_matches):
            log_fn("  WARN: SkyrimVR.bak could not be identified as an "
                   "Amethyst launcher backup; leaving it intact.")
            return
        if loader.is_file() and filecmp.cmp(backup, loader, shallow=False):
            from Utils.deployment import RestoreIncompleteError
            raise RestoreIncompleteError(
                "SkyrimVR.bak contains the SKSEVR loader, not the game. "
                "Restore the original SkyrimVR.exe before deploying.")
        if override_matches:
            original_ini = ini_path.read_bytes()
            if ini_path.is_symlink():
                write_atomic(ini_path, original_ini)
            _set_ini_key(ini_path, "Loader", "RuntimeName", None,
                         case_insensitive=True)
        try:
            backup.replace(runtime)
        except OSError:
            if override_matches:
                write_atomic(ini_path, original_ini)
            raise
        if override_matches:
            log_fn("  Removed Amethyst RuntimeName from Data/SKSE/skse.ini.")
        log_fn(f"  Restored {runtime.name} from {backup.name}.")

    def swap_launcher(self, log_fn) -> None:
        self._restore_launcher(log_fn)
        log_fn("  Skyrim VR uses sksevr_loader.exe directly; launcher swap skipped.")

    def post_deploy(self, log_fn=None) -> None:
        super().post_deploy(log_fn=log_fn)
        if self.vfs_launch_enabled:
            return
        paths = self._script_extender_paths()
        if paths is None or not paths[0].is_symlink():
            return
        loader = paths[0]
        fd, temp_name = tempfile.mkstemp(
            prefix=f".{loader.name}.amethyst-", dir=loader.parent)
        os.close(fd)
        temporary = Path(temp_name)
        try:
            shutil.copy2(loader, temporary)
            temporary.replace(loader)
        finally:
            temporary.unlink(missing_ok=True)
        if log_fn:
            log_fn("  Materialized SKSEVR loader in the game directory.")
