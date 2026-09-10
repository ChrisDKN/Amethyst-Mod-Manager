"""
oblivion_remastered.py
Game handler for Oblivion Remastered (Unreal Engine 5).

Mod structure
-------------
Mods ship files destined for multiple locations in the game root:

  .esp / .esm         → OblivionRemastered/Content/Dev/ObvData/Data/
  .pak / .utoc / .ucas → OblivionRemastered/Content/Paks/
  ue4ss/ folder        → OblivionRemastered/Binaries/Win64/
  .lua files           → OblivionRemastered/Binaries/Win64/ue4ss/Mods/
  OBSE/ folder         → OblivionRemastered/Binaries/Win64/
  .bk2 cutscenes       → OblivionRemastered/Content/Movies/Modern/
  Loose files (no rule) → game root (OblivionRemastered/)

The game root itself lives inside the Steam install directory:
  <steam_install>/OblivionRemastered/

Plugins.txt is managed by the plugin panel (extensions: .esp, .esm).
"""

from __future__ import annotations

import filecmp
import os
import shutil
import tempfile
from pathlib import Path

from Games.ue5_game import UE5Game, UE5Rule
from Games.Bethesda.bethesda_ini import _read_ini_key, _set_ini_key
from Utils.deployment import CustomRule, LinkMode
from Utils.config_paths import get_profiles_dir
from Utils.atomic_write import write_atomic

# Plugins.txt lives here inside the game root (OblivionRemastered/)
_PLUGINS_TXT_GAME_REL = Path("Content/Dev/ObvData/Data/Plugins.txt")

_PROFILES_DIR = get_profiles_dir()

# Game root subfolder inside the Steam install directory
_GAME_SUBDIR = "OblivionRemastered"


class OblivionRemastered(UE5Game):

    supports_script_extender_swap = False

    vanilla_plugins = [
        "Oblivion.esm",
        "DLCBattlehornCastle.esp", "DLCFrostcrag.esp", "DLCHorseArmor.esp",
        "DLCMehrunesRazor.esp", "DLCOrrery.esp", "DLCShiveringIsles.esp",
        "DLCSpellTomes.esp", "DLCThievesDen.esp", "DLCVileLair.esp",
        "Knights.esp",
        "AltarESPMain.esp", "AltarDeluxe.esp", "AltarESPLocal.esp",
    ]

    # Data-folder mods (often ported from original Oblivion) can still be
    # authored as BAIN packages, so keep the picker despite the UE5 base.
    @property
    def supports_bain(self) -> bool:
        return True

    # -----------------------------------------------------------------------
    # Identity
    # -----------------------------------------------------------------------

    @property
    def name(self) -> str:
        return "Oblivion Remastered"

    @property
    def game_id(self) -> str:
        return "oblivion_remastered"

    @property
    def exe_name(self) -> str:
        return "OblivionRemastered/Binaries/Win64/OblivionRemastered-Win64-Shipping.exe"

    @property
    def steam_id(self) -> str:
        return "2623190"

    @property
    def nexus_game_domain(self) -> str:
        return "oblivionremastered"

    @property
    def plugin_extensions(self) -> list[str]:
        return [".esp", ".esm"]

    @property
    def plugins_use_star_prefix(self) -> bool:
        return False

    @property
    def plugins_include_vanilla(self) -> bool:
        return True
    
    @property
    def mod_required_top_level_folders(self) -> set[str]:
        return {"OblivionRemastered", "Content", "Paks", "~mods", "Binaries", "Win64","ue4ss","Mods"}

    @property
    def mod_auto_strip_until_required(self) -> bool:
        return True

    @property
    def mod_install_as_is_if_no_match(self) -> bool:
        return True

    @property
    def mod_folder_strip_prefixes_post(self) -> set[str]:
        return {"OblivionRemastered", "Content", "Paks", "~mods", "Binaries", "Win64","ue4ss","Mods"}
    
    @property
    def conflict_ignore_filenames(self) -> set[str]:
        return {"*.html","*.md","*.jpeg","*.png","*.jpg","*.rar","*read*.txt","*.docx","*install*.txt","*guide*.txt"}
    
    @property
    def loot_sort_enabled(self) -> bool:
        return True
    
    @property
    def loot_game_type(self) -> str:
        return "OblivionRemastered"
    
    @property
    def frameworks(self) -> dict[str, str]:
        return {
            "Script Extender": "Binaries/Win64/obse64_loader.exe",
            "UE4SS": "Binaries/Win64/dwmapi.dll"
            }

    @property
    def _script_extender_exe(self) -> str:
        return "Binaries/Win64/obse64_loader.exe"

    @property
    def wine_dll_overrides(self) -> dict[str, str]:
        return {"dwmapi": "native,builtin", "winmm": "native,builtin"}

    @property
    def framework_launch_exes(self) -> dict[str, str]:
        return {"Script Extender": self._script_extender_exe}

    def get_launch_handoff(self, profile: str | None = None):
        if self.vfs_launch_enabled:
            return super().get_launch_handoff(profile)
        paths = self._script_extender_paths()
        if paths is None or not paths[0].is_file():
            return None
        from Utils.launchers.handoff import LaunchHandoff, LaunchHandoffField
        command = (
            "bash -c 'exec \"${@/OblivionRemastered.exe/"
            "OblivionRemastered/Binaries/Win64/obse64_loader.exe}\"' "
            "-- %command%"
        )
        return LaunchHandoff(
            launcher_id="steam-obse64",
            launcher_name="Steam",
            instructions=(
                "Open Properties → General and paste this into Launch Options."
            ),
            fields=(LaunchHandoffField("Launch Options", command),),
            note=(
                "Set this once to make Steam launch the deployed OBSE64 loader. "
                "Amethyst's obse64_loader.exe Run entry launches it directly "
                "and does not require this setting."
            ),
        )

    def _script_extender_paths(self) -> tuple[Path, Path, Path] | None:
        game_path = self.get_game_path()
        if game_path is None:
            return None
        bin_dir = game_path / "Binaries" / "Win64"
        runtime = bin_dir / "OblivionRemastered-Win64-Shipping.exe"
        backup = runtime.with_name(runtime.stem + ".bak")
        return bin_dir / "obse64_loader.exe", runtime, backup

    def _script_extender_runtime_ini_path(self) -> Path | None:
        game_path = self.get_game_path()
        if game_path is None:
            return None
        from Utils.games.frameworks import resolve_file_ci
        relative = Path("Binaries/Win64/OBSE/obse.ini")
        return resolve_file_ci(game_path, relative) or game_path / relative

    def _remove_script_extender_runtime_override(self, log_fn) -> None:
        paths = self._script_extender_paths()
        ini_path = self._script_extender_runtime_ini_path()
        if paths is None or ini_path is None:
            return
        _loader, _runtime, backup = paths
        if (not backup.is_file()
                or _read_ini_key(
                    ini_path, "Loader", "RuntimeName",
                    case_insensitive=True) != backup.name):
            return
        if ini_path.is_symlink():
            write_atomic(ini_path, ini_path.read_bytes())
        _set_ini_key(ini_path, "Loader", "RuntimeName", None,
                     case_insensitive=True)
        log_fn("  Removed Amethyst RuntimeName from "
               "Binaries/Win64/OBSE/obse.ini.")

    def _materialize_script_extender_loader(self, log_fn=None) -> None:
        _log = log_fn or (lambda _: None)
        paths = self._script_extender_paths()
        if paths is None or self.vfs_launch_enabled:
            return
        loader, _runtime, _backup = paths
        if not loader.is_symlink():
            return
        if not loader.is_file():
            raise FileNotFoundError(
                f"OBSE64 loader symlink is broken: {loader}")
        fd, temp_name = tempfile.mkstemp(
            prefix=f".{loader.name}.amethyst-", dir=loader.parent)
        os.close(fd)
        temp_path = Path(temp_name)
        try:
            shutil.copy2(loader, temp_path)
            os.replace(temp_path, loader)
        finally:
            temp_path.unlink(missing_ok=True)
        _log("  Materialized Binaries/Win64/obse64_loader.exe so OBSE64 "
             "resolves the game runtime from the deployed directory.")

    def post_deploy(self, log_fn=None) -> None:
        super().post_deploy(log_fn=log_fn)
        try:
            self._materialize_script_extender_loader(log_fn)
        except Exception:
            self.add_deploy_warning(
                "OBSE64 could not be prepared as a real file in Binaries/Win64; "
                "direct script-extender launch may fail. See the deploy log.")
            raise

    def _restore_launcher(self, log_fn=None) -> None:
        _log = log_fn or (lambda _: None)
        paths = self._script_extender_paths()
        if paths is None:
            return
        loader, runtime, backup = paths
        if not backup.is_file():
            return
        ini_path = self._script_extender_runtime_ini_path()
        override_matches = (
            _read_ini_key(
                ini_path, "Loader", "RuntimeName", case_insensitive=True)
            == backup.name
            if ini_path is not None else False
        )
        try:
            runtime_matches = (
                runtime.is_file()
                and loader.is_file()
                and filecmp.cmp(runtime, loader, shallow=False)
            )
        except OSError:
            runtime_matches = False
        if not (override_matches or runtime_matches):
            _log("  WARN: an existing Oblivion Remastered runtime backup was "
                 "not created by Amethyst's legacy launcher swap; leaving it "
                 "intact.")
            return
        self._remove_script_extender_runtime_override(_log)
        if runtime.is_file() or runtime.is_symlink():
            runtime.unlink()
        backup.rename(runtime)
        _log(f"  Restored {runtime.name} from {backup.name}.")
    
    @property
    def loot_masterlist_repo(self) -> str:
        return "oblivion-remastered"

    # -----------------------------------------------------------------------
    # Routing rules
    # -----------------------------------------------------------------------
    # Game-root file routing is declared via ``custom_routing_rules``
    # (CustomRule), the same mechanism Marvel Rivals uses.  Rules are evaluated
    # in order; the first match wins.

    @property
    def custom_routing_rules(self) -> list[CustomRule]:
        return [
            CustomRule(dest="Unused",
                        folders=["wingdk", "src", "True Oblivion.ini Merger"]),

            CustomRule(dest="Unused", 
                        filenames=["Altar.ini"],
                        flatten=True),
            
            # Required as our strip prefix rules do not apply to fomods
            CustomRule(dest="", 
                        folders=["Content", "Binaries"], 
                        flatten=True),

            CustomRule(dest="Binaries/Win64", 
                        filenames=["*obse64_*.*"],
                        flatten=True),

            CustomRule(dest="Content/Paks", 
                        folders=["LogicMods"], 
                        flatten=True),

            CustomRule(dest="Binaries/Win64",
                        folders=["ue4ss", "obse", "GameSettings",
                                "MadConfigs", "SkipMessages"],
                        flatten=True),

            CustomRule(dest="Content/Paks/~mods", 
                        extensions=[".pak"],
                        companion_extensions=[".ucas", ".utoc"],
                        include_siblings=True),

            CustomRule(dest="Binaries/Win64/ue4ss/Mods",
                        folders=["scripts", "dlls"], 
                        include_siblings=True),
        
            CustomRule(dest="Binaries/Win64/ue4ss/Mods",
                        filenames=["enabled.txt"], 
                        include_siblings=True),

            CustomRule(dest="Binaries/Win64/ue4ss/Mods",
                        filenames=["mods.txt"], 
                        flatten=True),

            CustomRule(dest="Content/Dev/ObvData/Data",
                        extensions=[".esm", ".esp"], 
                        flatten=True),

            CustomRule(dest="Content/Dev/ObvData/Data",
                        folders=["MagicLoader", "bashtags","SyncMap","sound"], 
                        flatten=True),

            CustomRule(dest="Content/Movies/Modern", 
                        extensions=[".bk2"],
                        flatten=True),

            CustomRule(dest="Binaries/Win64/ue4ss/Mods",
                        folders=["shared", "NPCAppearanceManager"], 
                        flatten=True),

            CustomRule(dest="Unused",
                        extensions=[".txt"], 
                        loose_only=True),
        ]

    @property
    def _ue5_post_passthrough_rules(self) -> list[UE5Rule]:
        # Trailing UE5 fallbacks (same as Ue5CustomGame's defaults) - applied
        # before the custom_routing_rules - Used to apply the ue4ss base structure
        return [
            UE5Rule(
                dest="Binaries/Win64",
                filenames=["dwmapi.dll"],
            ),
            UE5Rule(
                dest="Binaries/Win64/ue4ss",
                filenames=["ue4ss.dll", "ue4ss.pdb", "ue4ss-settings.ini"],
            ),
        ]

    # -----------------------------------------------------------------------
    # Paths
    # -----------------------------------------------------------------------

    def get_vanilla_plugins_path(self) -> Path | None:
        game_path = self.get_game_path()
        if game_path is None:
            return None
        return game_path / _PLUGINS_TXT_GAME_REL.parent

    def get_game_path(self) -> Path | None:
        """The actual game root is the OblivionRemastered/ subfolder inside
        the Steam install directory.  Looked up case-insensitively so it works
        on both Windows (via Proton) and Linux test setups."""
        if self._game_path is None:
            return None
        # Exact match first
        sub = self._game_path / _GAME_SUBDIR
        if sub.is_dir():
            return sub
        # Case-insensitive scan (handles lowercase 'oblivionremastered' on Linux)
        needle = _GAME_SUBDIR.lower()
        try:
            for child in self._game_path.iterdir():
                if child.is_dir() and child.name.lower() == needle:
                    return child
        except OSError:
            pass
        # Fallback: user pointed directly at the subfolder
        return self._game_path

    def get_mod_staging_path(self) -> Path:
        if self._staging_path is not None:
            return self._staging_path / "mods"
        return _PROFILES_DIR / self.name / "mods"

    # -----------------------------------------------------------------------
    # Deploy / restore (adds Plugins.txt symlink)
    # -----------------------------------------------------------------------

    def _plugins_txt_target(self) -> Path | None:
        game_path = self.get_game_path()
        if game_path is None:
            return None
        return game_path / _PLUGINS_TXT_GAME_REL

    def _symlink_plugins_txt(self, profile: str, log_fn) -> None:
        _log = log_fn
        target = self._plugins_txt_target()
        if target is None:
            return
        source = self.get_profile_root() / "profiles" / profile / "plugins.txt"
        if not source.is_file():
            _log(f"  WARN: plugins.txt not found at {source} - skipping deploy.")
            return
        from Utils.plugins import deploy_plugins_copy
        content = source.read_text(encoding="utf-8")
        deploy_plugins_copy(target.parent, target.name, content, _log)

    def _remove_plugins_txt_symlink(self, log_fn) -> None:
        from Utils.plugins import remove_plugins_copy
        target = self._plugins_txt_target()
        if target is not None:
            remove_plugins_copy(target.parent, target.name, log_fn)

    def _vfs_populate_ue5_layer_files(
        self, destination: Path, profile: str, log_fn,
    ) -> None:
        """Generate Plugins.txt inside the private UE project layer."""
        source = self.get_profile_root() / "profiles" / profile / "plugins.txt"
        if not source.is_file():
            log_fn(f"  WARN: plugins.txt not found at {source} - skipping deploy.")
            return
        from Utils.plugins import deploy_plugins_copy
        target = Path(destination) / _PLUGINS_TXT_GAME_REL
        content = source.read_text(encoding="utf-8")
        deploy_plugins_copy(target.parent, target.name, content, log_fn)

    def deploy(self, log_fn=None, mode: LinkMode = LinkMode.HARDLINK,
               profile: str = "default", progress_fn=None) -> None:
        super().deploy(log_fn=log_fn, mode=mode, profile=profile, progress_fn=progress_fn)
        if self.vfs_launch_enabled:
            return
        _log = log_fn or (lambda _: None)
        _log("Symlinking Plugins.txt ...")
        self._symlink_plugins_txt(profile, _log)

    def restore(self, log_fn=None, progress_fn=None) -> None:
        from Utils.vfs import has_deployment_state
        _log = log_fn or (lambda _: None)
        had_physical = self._ue5_deployed_manifest_path().is_file()
        was_vfs = (
            has_deployment_state(self)
            or self._vfs_external_manifest_path().exists()
            or self._vfs_prefix_context_path().exists()
        )
        self._restore_launcher(_log)
        super().restore(log_fn=log_fn, progress_fn=progress_fn)
        if had_physical or not was_vfs:
            self._remove_plugins_txt_symlink(_log)
