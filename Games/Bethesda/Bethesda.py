"""
Bethesda.py
Game handler for Various Bethesda games using the same deployment method.

Mod structure:
  Mods install into <game_path>/Data/
  Staged mods live in Profiles/Fallout 3/mods/
"""

import json
import shutil
from pathlib import Path

from Games.base_game import BaseGame, WizardTool
from Utils.deploy import LinkMode, deploy_core, deploy_filemap, move_to_core, restore_data_core
from Utils.config_paths import get_profiles_dir
from Utils.steam_finder import find_prefix

_PROFILES_DIR = get_profiles_dir()


class Fallout_3(BaseGame):

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
    def loot_sort_enabled(self) -> bool:
        return True

    @property
    def loot_game_type(self) -> str:
        return "fo3"

    @property
    def loot_masterlist_url(self) -> str:
        return "https://raw.githubusercontent.com/loot/fallout3/v0.26/masterlist.yaml"

    @property
    def wizard_tools(self) -> list[WizardTool]:
        return [
            WizardTool(
                id="downgrade_fo3",
                label="Downgrade Fallout 3",
                description=(
                    "Downgrade to pre-Anniversary Edition so that "
                    "the script extender (FOSE) works correctly."
                ),
                dialog_class_path="gui.wizard_fallout_downgrade.FalloutDowngradeWizard",
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
                "copy":    LinkMode.COPY,
            }.get(raw_mode, LinkMode.HARDLINK)
            raw_staging = data.get("staging_path", "")
            if raw_staging:
                self._staging_path = Path(raw_staging)
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
            "game_path":    str(self._game_path)    if self._game_path    else "",
            "prefix_path":  str(self._prefix_path)  if self._prefix_path  else "",
            "deploy_mode":  mode_str,
            "staging_path": str(self._staging_path) if self._staging_path else "",
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

    def set_prefix_path(self, path: Path | str | None) -> None:
        self._prefix_path = Path(path) if path else None
        self.save_paths()

    # -----------------------------------------------------------------------
    # Deployment
    # -----------------------------------------------------------------------

    _APPDATA_SUBPATH = Path("drive_c/users/steamuser/AppData/Local/Fallout3")

    @property
    def _script_extender_exe(self) -> str:
        return "fose_loader.exe"

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

    def _swap_launcher(self, log_fn) -> None:
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
        filemap  = self.get_profile_root() / "filemap.txt"
        staging  = self.get_mod_staging_path()

        if not data_dir.is_dir():
            raise RuntimeError(f"Data directory not found: {data_dir}")
        if not filemap.is_file():
            raise RuntimeError(
                f"filemap.txt not found: {filemap}\n"
                "Run 'Build Filemap' before deploying."
            )

        _log("Step 1: Moving Data/ → Data_Core/ ...")
        moved = move_to_core(data_dir, log_fn=_log)
        _log(f"  Moved {moved} file(s) to Data_Core/.")

        _log(f"Step 2: Transferring mod files into Data/ ({mode.name}) ...")
        linked_mod, placed = deploy_filemap(filemap, data_dir, staging,
                                            mode=mode,
                                            strip_prefixes=self.mod_folder_strip_prefixes,
                                            log_fn=_log,
                                            progress_fn=progress_fn)
        _log(f"  Transferred {linked_mod} mod file(s).")

        _log("Step 3: Filling gaps with vanilla files from Data_Core/ ...")
        linked_core = deploy_core(data_dir, placed, mode=mode, log_fn=_log)
        _log(f"  Transferred {linked_core} vanilla file(s).")

        _log("Step 4: Symlinking plugins.txt into Proton prefix ...")
        self._symlink_plugins_txt(profile, _log)

        _log(f"Step 5: Swapping launcher for {self._script_extender_exe} ...")
        self._swap_launcher(_log)

        _log(
            f"Deploy complete. "
            f"{linked_mod} mod + {linked_core} vanilla "
            f"= {linked_mod + linked_core} total file(s) in Data/."
        )

    def restore(self, log_fn=None) -> None:
        """Restore Data/ to its vanilla state by moving Data_Core/ back."""
        _log = log_fn or (lambda _: None)

        if self._game_path is None:
            raise RuntimeError("Game path is not configured.")

        data_dir = self._game_path / "Data"

        _log("Restore: clearing Data/ and moving Data_Core/ back ...")
        restored = restore_data_core(data_dir, log_fn=_log)
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


class Fallout_NV(Fallout_3):

    @property
    def wizard_tools(self) -> list[WizardTool]:
        return []

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
        return "fonv"

    @property
    def loot_masterlist_url(self) -> str:
        return "https://raw.githubusercontent.com/loot/falloutnv/v0.26/masterlist.yaml"

    _APPDATA_SUBPATH = Path("drive_c/users/steamuser/AppData/Local/FalloutNV")

    @property
    def _script_extender_exe(self) -> str:
        return "nvse_loader.exe"


class Fallout_4(Fallout_3):

    @property
    def wizard_tools(self) -> list[WizardTool]:
        return []

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
    def loot_masterlist_url(self) -> str:
        return "https://raw.githubusercontent.com/loot/fallout4/v0.21/masterlist.yaml"

    _APPDATA_SUBPATH = Path("drive_c/users/steamuser/AppData/Local/Fallout4")

    @property
    def _script_extender_exe(self) -> str:
        return "f4se_loader.exe"


class Fallout_4VR(Fallout_3):

    @property
    def wizard_tools(self) -> list[WizardTool]:
        return []

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
    def loot_game_type(self) -> str:
        return "Fallout4VR"

    @property
    def loot_masterlist_url(self) -> str:
        return "https://raw.githubusercontent.com/loot/fallout4/v0.21/masterlist.yaml"

    _APPDATA_SUBPATH = Path("drive_c/users/steamuser/AppData/Local/Fallout4VR")

    @property
    def _script_extender_exe(self) -> str:
        return "f4sevr_loader.exe"


class Oblivion(Fallout_3):

    @property
    def wizard_tools(self) -> list[WizardTool]:
        return []

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

    _APPDATA_SUBPATH = Path("drive_c/users/steamuser/AppData/Local/Oblivion")

    @property
    def _script_extender_exe(self) -> str:
        return "obse_loader.exe"


class Skyrim(Fallout_3):

    @property
    def wizard_tools(self) -> list[WizardTool]:
        return []

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

    _APPDATA_SUBPATH = Path("drive_c/users/steamuser/AppData/Local/Skyrim")

    @property
    def _script_extender_exe(self) -> str:
        return "skse_loader.exe"


class SkyrimVR(Fallout_3):

    @property
    def wizard_tools(self) -> list[WizardTool]:
        return []

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
        return "https://raw.githubusercontent.com/loot/skyrimse/v0.21/masterlist.yaml"

    _APPDATA_SUBPATH = Path("drive_c/users/steamuser/AppData/Local/Skyrim VR")

    @property
    def _script_extender_exe(self) -> str:
        return "sksevr_loader.exe"


class Starfield(Fallout_3):

    @property
    def wizard_tools(self) -> list[WizardTool]:
        return []

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
    def plugin_extensions(self) -> list[str]:
        # .esp support was added alongside native plugins.txt support in patch 1.12.30 (June 2024).
        return [".esp", ".esl", ".esm"]

    @property
    def steam_id(self) -> str:
        return "1716740"

    @property
    def nexus_game_domain(self) -> str:
        return "starfield"

    @property
    def loot_sort_enabled(self) -> bool:
        return False

    # plugins.txt lives at AppData/Local/Starfield/plugins.txt — same pattern as other Bethesda titles.
    _APPDATA_SUBPATH = Path("drive_c/users/steamuser/AppData/Local/Starfield")

    @property
    def _script_extender_exe(self) -> str:
        return "sfse_loader.exe"
