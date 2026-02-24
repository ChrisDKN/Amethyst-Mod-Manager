"""
baldurs_gate_3.py
Game handler for Baldur's Gate 3.

Mod structure:
  Mods install into the Proton prefix AppData folder:
    drive_c/users/steamuser/AppData/Local/Larian Studios/Baldur's Gate 3/Mods/
  Staged mods live in Profiles/Baldur's Gate 3/mods/

  Only .pak files are deployed to the Mods folder — other files (readmes,
  images, etc.) are excluded from the filemap via mod_install_extensions.

  Mods that contain a bin/ folder have those files deployed to the game's
  root install directory instead (via filemap_root.txt), since BG3 native
  mods expect bin/ to sit alongside the game executable.

  After deploying .pak files, modsettings.lsx is generated automatically so
  BG3 recognises the installed mods.  Mod load order follows the modlist
  priority, with dependencies topologically sorted to appear before the
  mods that require them.
"""

import json
from pathlib import Path

from Games.base_game import BaseGame
from Utils.deploy import (
    LinkMode, deploy_filemap, deploy_core, move_to_core, restore_data_core,
    deploy_filemap_to_root, load_per_mod_strip_prefixes, restore_filemap_from_root,
)
from Utils.config_paths import get_profiles_dir
from Utils.modsettings import write_modsettings, write_vanilla_modsettings
from Utils.steam_finder import find_prefix

_PROFILES_DIR = get_profiles_dir()

# Path inside the Proton prefix where BG3 reads mods from
_MODS_SUBPATH = Path(
    "drive_c/users/steamuser/AppData/Local/Larian Studios/Baldur's Gate 3/Mods"
)

# Path inside the Proton prefix where BG3 reads modsettings.lsx
_MODSETTINGS_SUBPATH = Path(
    "drive_c/users/steamuser/AppData/Local/Larian Studios"
    "/Baldur's Gate 3/PlayerProfiles/Public/modsettings.lsx"
)


class BaldursGate3(BaseGame):

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
        return "Baldur's Gate 3"

    @property
    def game_id(self) -> str:
        return "baldurs_gate_3"

    @property
    def exe_name(self) -> str:
        return "bin/bg3.exe"

    @property
    def steam_id(self) -> str:
        return "1086940"

    @property
    def nexus_game_domain(self) -> str:
        return "baldursgate3"

    @property
    def mod_folder_strip_prefixes(self) -> set[str]:
        return {"mods"}

    @property
    def mod_root_deploy_folders(self) -> set[str]:
        return {"bin"}

    @property
    def mod_install_extensions(self) -> set[str]:
        return {".pak",".json",".lsx"}

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
        """Mods deploy into the Proton prefix AppData Mods folder."""
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
        """Deploy staged .pak mods into the Proton prefix Mods folder.

        Workflow:
          1. Move everything currently in the Mods folder → Mods_Core/
          2. Hard-link every .pak listed in filemap.txt into the Mods folder
          3. Hard-link vanilla .pak files from Mods_Core/ for anything not
             provided by a mod
          4. Generate modsettings.lsx so BG3 recognises the installed mods
        (Root Folder deployment is handled by the GUI after this returns.)
        """
        _log = log_fn or (lambda _: None)

        if self._prefix_path is None:
            raise RuntimeError("Prefix path is not configured.")

        mods_dir = self._prefix_path / _MODS_SUBPATH
        filemap  = self.get_profile_root() / "filemap.txt"
        staging  = self.get_mod_staging_path()
        modlist  = self.get_profile_root() / "profiles" / profile / "modlist.txt"

        mods_dir.mkdir(parents=True, exist_ok=True)

        if not filemap.is_file():
            raise RuntimeError(
                f"filemap.txt not found: {filemap}\n"
                "Run 'Build Filemap' before deploying."
            )

        _log("Step 1: Moving Mods/ → Mods_Core/ ...")
        moved = move_to_core(mods_dir, log_fn=_log)
        _log(f"  Moved {moved} file(s) to Mods_Core/.")

        _log(f"Step 2: Transferring mod .pak files into Mods/ ({mode.name}) ...")
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

        # Step 4 (optional): deploy root-targeted files (e.g. bin/) to game root
        linked_root = 0
        filemap_root = self.get_profile_root() / "filemap_root.txt"
        if filemap_root.is_file() and self._game_path:
            _log("Step 4: Deploying root-targeted files (bin/, …) to game root ...")
            linked_root, _ = deploy_filemap_to_root(
                filemap_root, self._game_path, staging,
                mode=mode, strip_prefixes=self.mod_folder_strip_prefixes,
                per_mod_strip_prefixes=per_mod_strip,
                log_fn=_log, progress_fn=progress_fn,
            )
            _log(f"  Transferred {linked_root} file(s) to game root.")

        _log("Step 5: Generating modsettings.lsx ...")
        modsettings = self._prefix_path / _MODSETTINGS_SUBPATH
        game_data = self._game_path / "Data" if self._game_path else None
        mod_count = write_modsettings(modsettings, modlist, staging,
                                      log_fn=_log,
                                      game_data_path=game_data)

        _log(
            f"Deploy complete. "
            f"{linked_mod} mod + {linked_core} vanilla "
            f"= {linked_mod + linked_core} total file(s) in Mods/. "
            f"{linked_root} file(s) deployed to game root. "
            f"modsettings.lsx written with {mod_count} mod(s)."
        )

    def restore(self, log_fn=None) -> None:
        """Remove deployed mods and restore the vanilla Mods folder."""
        _log = log_fn or (lambda _: None)

        if self._prefix_path is None:
            raise RuntimeError("Prefix path is not configured.")

        mods_dir = self._prefix_path / _MODS_SUBPATH

        # Undo root-targeted files (bin/, …) placed into game root
        filemap_root = self.get_profile_root() / "filemap_root.txt"
        if self._game_path:
            removed_root = restore_filemap_from_root(
                filemap_root, self._game_path, log_fn=_log,
            )
            if removed_root:
                _log(f"  Removed {removed_root} root-deployed file(s).")

        _log("Restore: clearing Mods/ and moving Mods_Core/ back ...")
        restored = restore_data_core(mods_dir, log_fn=_log)
        _log(f"  Restored {restored} file(s). Mods_Core/ removed.")

        _log("Restore: resetting modsettings.lsx to vanilla ...")
        modsettings = self._prefix_path / _MODSETTINGS_SUBPATH
        write_vanilla_modsettings(modsettings, log_fn=_log)

        _log("Restore complete.")
