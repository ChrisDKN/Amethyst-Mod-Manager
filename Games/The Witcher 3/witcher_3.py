"""
witcher_3.py
Game handler for The Witcher 3: Wild Hunt.

Mod structure:
  Mods install directly into the game root (mods/, bin/, etc.)
  Staged mods live in Profiles/The Witcher 3/mods/

  Most mods ship as <ModName>/content/… and are auto-prefixed under mods/.
  Mods that already ship with a mods/ or bin/ top-level folder are left as-is.
"""

import json
from pathlib import Path

from Games.base_game import BaseGame
from Utils.deploy import LinkMode, deploy_filemap_to_root, restore_filemap_from_root
from Utils.config_paths import get_profiles_dir
from Utils.steam_finder import find_prefix

_PROFILES_DIR = get_profiles_dir()


class Witcher3(BaseGame):

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
        return "The Witcher 3"

    @property
    def game_id(self) -> str:
        return "witcher_3"

    @property
    def exe_name(self) -> str:
        return "witcher3.exe"
    
    @property
    def heroic_app_names(self) -> list[str]:
        return ["1207658924", "The Witcher 3: Wild Hunt"]  # GOG ID + title fallback

    @property
    def steam_id(self) -> str:
        return "292030"

    @property
    def nexus_game_domain(self) -> str:
        return "witcher3"

    @property
    def mod_install_prefix(self) -> str:
        return "mods"

    @property
    def mod_required_top_level_folders(self) -> set[str]:
        return {"mods", "bin"}

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
        """Mods deploy directly into the game root (mods/, bin/, etc.)."""
        return self._game_path

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
        """Deploy staged mods directly into the game root.

        Workflow:
          1. Back up any vanilla files that mod files will overwrite
          2. Transfer mod files listed in filemap.txt into the game root
        (Root Folder deployment is handled by the GUI after this returns.)
        """
        _log = log_fn or (lambda _: None)

        if self._game_path is None:
            raise RuntimeError("Game path is not configured.")

        game_root = self._game_path
        filemap   = self.get_profile_root() / "filemap.txt"
        staging   = self.get_mod_staging_path()

        if not filemap.is_file():
            raise RuntimeError(
                f"filemap.txt not found: {filemap}\n"
                "Run 'Build Filemap' before deploying."
            )

        _log(f"Transferring mod files into game root ({mode.name}) ...")
        linked_mod, _ = deploy_filemap_to_root(filemap, game_root, staging,
                                               mode=mode,
                                               strip_prefixes=self.mod_folder_strip_prefixes,
                                               log_fn=_log,
                                               progress_fn=progress_fn)
        _log(f"Deploy complete. {linked_mod} mod file(s) placed in game root.")

    def restore(self, log_fn=None) -> None:
        """Remove deployed mod files from the game root and restore any vanilla files."""
        _log = log_fn or (lambda _: None)

        if self._game_path is None:
            raise RuntimeError("Game path is not configured.")

        filemap   = self.get_profile_root() / "filemap.txt"
        game_root = self._game_path

        _log("Restore: removing mod files and restoring vanilla files ...")
        removed = restore_filemap_from_root(filemap, game_root, log_fn=_log)
        _log(f"Restore complete. {removed} mod file(s) removed from game root.")
