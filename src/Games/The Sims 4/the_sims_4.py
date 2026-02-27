"""
the_sims_4.py
Game handler for The Sims 4.

Mod structure:
  Mods install into the Proton prefix Documents folder:
    drive_c/users/steamuser/Documents/Electronic Arts/The Sims 4/Mods/
  Staged mods live in Profiles/The Sims 4/mods/

  Only .package and .ts4script files are deployed — other files (readmes,
  images, etc.) are excluded via mod_install_extensions.
"""

import json
from pathlib import Path

from Games.base_game import BaseGame
from Utils.deploy import LinkMode, deploy_filemap, deploy_core, load_per_mod_strip_prefixes, move_to_core, restore_data_core
from Utils.config_paths import get_profiles_dir
from Utils.steam_finder import find_prefix

_PROFILES_DIR = get_profiles_dir()

# Path inside the Proton prefix where The Sims 4 reads mods from
_MODS_SUBPATH = Path(
    "drive_c/users/steamuser/Documents/Electronic Arts/The Sims 4/Mods"
)


class TheSims4(BaseGame):

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
        return "The Sims 4"

    @property
    def game_id(self) -> str:
        return "the_sims_4"

    @property
    def exe_name(self) -> str:
        return "TS4_x64.exe"

    @property
    def steam_id(self) -> str:
        return "1222670"

    @property
    def nexus_game_domain(self) -> str:
        return "thesims4"

    @property
    def mod_install_extensions(self) -> set[str]:
        return {".package", ".ts4script"}

    @property
    def plugin_extensions(self) -> list[str]:
        return []

    @property
    def loot_sort_enabled(self) -> bool:
        return False

    @property
    def loot_game_type(self) -> str:
        return ""

    @property
    def loot_masterlist_url(self) -> str:
        return ""

    # -----------------------------------------------------------------------
    # Paths
    # -----------------------------------------------------------------------

    def get_game_path(self) -> Path | None:
        return self._game_path

    def get_mod_data_path(self) -> Path | None:
        """Mods deploy into the Proton prefix Documents Mods folder."""
        if self._prefix_path is None:
            return None
        return self._prefix_path / _MODS_SUBPATH

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

    def deploy(self, log_fn=None, mode: LinkMode = LinkMode.HARDLINK,
               profile: str = "default", progress_fn=None) -> None:
        """Deploy staged mods into the Proton prefix Mods folder.

        Workflow:
          1. Move everything currently in the Mods folder → Mods_Core/
          2. Hard-link every .package/.ts4script listed in filemap.txt into Mods/
          3. Hard-link vanilla files from Mods_Core/ for anything not
             provided by a mod
        (Root Folder deployment is handled by the GUI after this returns.)
        """
        _log = log_fn or (lambda _: None)

        if self._prefix_path is None:
            raise RuntimeError("Prefix path is not configured.")

        mods_dir = self._prefix_path / _MODS_SUBPATH
        filemap  = self.get_profile_root() / "filemap.txt"
        staging  = self.get_mod_staging_path()

        mods_dir.mkdir(parents=True, exist_ok=True)

        if not filemap.is_file():
            raise RuntimeError(
                f"filemap.txt not found: {filemap}\n"
                "Run 'Build Filemap' before deploying."
            )

        _log("Step 1: Moving Mods/ → Mods_Core/ ...")
        moved = move_to_core(mods_dir, log_fn=_log)
        _log(f"  Moved {moved} file(s) to Mods_Core/.")

        _log(f"Step 2: Transferring mod files into Mods/ ({mode.name}) ...")
        profile_dir = self.get_profile_root() / "profiles" / profile
        per_mod_strip = load_per_mod_strip_prefixes(profile_dir)
        linked_mod, placed = deploy_filemap(filemap, mods_dir, staging,
                                            mode=mode,
                                            strip_prefixes=self.mod_folder_strip_prefixes,
                                            per_mod_strip_prefixes=per_mod_strip,
                                            log_fn=_log,
                                            progress_fn=progress_fn)
        _log(f"  Transferred {linked_mod} mod file(s).")

        _log("Step 3: Filling gaps with vanilla files from Mods_Core/ ...")
        linked_core = deploy_core(mods_dir, placed, mode=mode, log_fn=_log)
        _log(f"  Transferred {linked_core} vanilla file(s).")

        _log(
            f"Deploy complete. "
            f"{linked_mod} mod + {linked_core} vanilla "
            f"= {linked_mod + linked_core} total file(s) in Mods/."
        )

    def restore(self, log_fn=None, progress_fn=None) -> None:
        """Remove deployed mods and restore the vanilla Mods folder."""
        _log = log_fn or (lambda _: None)

        if self._prefix_path is None:
            raise RuntimeError("Prefix path is not configured.")

        mods_dir = self._prefix_path / _MODS_SUBPATH

        _log("Restore: clearing Mods/ and moving Mods_Core/ back ...")
        restored = restore_data_core(mods_dir, overwrite_dir=self.get_profile_root() / "overwrite", log_fn=_log)
        _log(f"  Restored {restored} file(s). Mods_Core/ removed.")

        _log("Restore complete.")
