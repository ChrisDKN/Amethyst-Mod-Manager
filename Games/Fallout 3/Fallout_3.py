"""
Fallout_3.py
Game handler for Fallout 3.

Mod structure:
  Mods install into <game_path>/Data/
  Staged mods live in Profiles/Fallout 3/mods/
"""

import json
import shutil
from pathlib import Path

from Games.base_game import BaseGame
from Utils.deploy import LinkMode, deploy_core, deploy_filemap, move_to_core, restore_data_core
from Utils.steam_finder import find_prefix

_PROJECT_ROOT = Path(__file__).parent.parent.parent
_PROFILES_DIR = _PROJECT_ROOT / "Profiles"


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
    def loot_sort_enabled(self) -> bool:
        return True

    @property
    def loot_game_type(self) -> str:
        return "fo3"

    @property
    def loot_masterlist_url(self) -> str:
        return "https://raw.githubusercontent.com/loot/fallout3/v0.26/masterlist.yaml"

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

    # The Fallout 3 AppData folder inside the Proton prefix where the game
    # reads plugins.txt from.
    _APPDATA_SUBPATH = Path("drive_c/users/steamuser/AppData/Local/Fallout3")

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
        """Replace Fallout3Launcher.exe with fose_loader.exe if present."""
        _log = log_fn
        if self._game_path is None:
            return
        fose = self._game_path / "fose_loader.exe"
        if not fose.is_file():
            _log("  FOSE loader not found — skipping launcher swap.")
            return
        launcher = self._game_path / "Fallout3Launcher.exe"
        backup   = self._game_path / "Fallout3Launcher.bak"
        if launcher.is_file():
            launcher.rename(backup)
            _log("  Renamed Fallout3Launcher.exe → Fallout3Launcher.bak.")
        shutil.copy2(fose, launcher)
        _log("  Copied fose_loader.exe → Fallout3Launcher.exe.")

    def _restore_launcher(self, log_fn) -> None:
        """Reverse the FOSE launcher swap if a backup exists."""
        _log = log_fn
        if self._game_path is None:
            return
        backup   = self._game_path / "Fallout3Launcher.bak"
        launcher = self._game_path / "Fallout3Launcher.exe"
        if not backup.is_file():
            return
        if launcher.is_file():
            launcher.unlink()
        backup.rename(launcher)
        _log("  Restored Fallout3Launcher.exe from .bak.")

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

        _log("Step 5: Swapping launcher for FOSE ...")
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
