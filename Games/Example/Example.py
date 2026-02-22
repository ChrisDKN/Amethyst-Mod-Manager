"""
Example.py
Game handler template — copy this folder and fill in the TODOs.

Mod structure (update the docstring to match your game):
  Mods install into <game_path>/???/
  Staged mods live in Profiles/Example/mods/

Notes:
  - Root Folder deployment is handled automatically by the GUI after
    deploy() returns. You do NOT need to call deploy_root_folder() here.
    Set root_folder_deploy_enabled = False to disable it for this game.
  - The progress_fn callback should be forwarded to deploy_filemap() so
    the GUI can show a progress bar during deployment.
  - The plugins.txt symlink and launcher swap are optional — only needed
    for games that read plugins.txt from a Proton prefix AppData folder
    or need an exe swap (e.g. SKSE, F4SE).
"""

import json
import shutil
from pathlib import Path

from Games.base_game import BaseGame
from Utils.deploy import LinkMode, deploy_core, deploy_filemap, move_to_core, restore_data_core
from Utils.config_paths import get_profiles_dir
from Utils.steam_finder import find_prefix

_PROFILES_DIR = get_profiles_dir()


class Example(BaseGame):

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
        return "Example"  # TODO: human-readable name, must match folder name

    @property
    def game_id(self) -> str:
        return "Example"  # TODO: filesystem-safe ID, matches .py filename

    @property
    def exe_name(self) -> str:
        return "Example.exe"  # TODO: launcher / main executable name

    @property
    def steam_id(self) -> str:
        return ""  # TODO: Steam App ID, e.g. "489830"; leave "" if not on Steam

    @property
    def mod_folder_strip_prefixes(self) -> set[str]:
        return set()  # TODO: e.g. {"plugins", "bepinex"} to strip redundant wrapper folders from nexsus downloads

    @property
    def mod_install_prefix(self) -> str:
        return ""  # TODO: e.g. "mods" to prepend every installed file with mods/ automatically

    @property
    def mod_install_extensions(self) -> set[str]:
        return set()  # TODO: e.g. {".pak"} to only include .pak files in the filemap; empty = all files

    @property
    def mod_required_top_level_folders(self) -> set[str]:
        return set()  # TODO: e.g. {"archive", "bin", "r6", "red4ext"} to prompt users when a mod has no recognised top-level folder

    @property
    def mods_dir(self) -> str:
        return "" # The place mods go into from root. Eg BepInEx/Plugins/ for BepInEx mods. If the game doesn't have a specific subfolder for mods, return "" and they will be deployed to the game root.
    
    @property
    def plugin_extensions(self) -> list[str]:
        return []  # TODO: e.g. [".esp", ".esl", ".esm"]; [] disables plugin panel

    @property
    def root_folder_deploy_enabled(self) -> bool:
        return True  # TODO: set False if writing to the game root is unsafe

    @property
    def loot_sort_enabled(self) -> bool:
        return False  # TODO: set True and fill loot_game_type / loot_masterlist_url

    @property
    def loot_game_type(self) -> str:
        return ""  # TODO: libloot GameType attribute, e.g. "SkyrimSE"

    @property
    def loot_masterlist_url(self) -> str:
        return ""  # TODO: raw URL to the LOOT masterlist YAML for this game

    # -----------------------------------------------------------------------
    # Paths
    # -----------------------------------------------------------------------

    def get_game_path(self) -> Path | None:
        return self._game_path

    def get_mod_data_path(self) -> Path | None:
        """Return the directory inside the game where mod files are installed."""
        if self._game_path is None:
            return None
        return self._game_path  # TODO: change to e.g. self._game_path / "Data"

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

    def deploy(self, log_fn=None, mode: LinkMode = LinkMode.HARDLINK,
               profile: str = "default", progress_fn=None) -> None:
        _log = log_fn or (lambda _: None)

        if self._game_path is None:
            raise RuntimeError("Game path is not configured.")

        # TODO: set this to the directory inside the game where mods are installed.
        # e.g. self._game_path / "Data" for Bethesda games,
        #      self._game_path / "Mods" for Stardew Valley, etc.
        mods_dir = self._game_path / "TODO_mods_dir"

        filemap = self.get_profile_root() / "filemap.txt"
        staging = self.get_mod_staging_path()

        # Step 1: Back up the vanilla mod folder so we can restore it later.
        # Moves mods_dir/ → mods_dir_Core/ (e.g. Data/ → Data_Core/).
        move_to_core(mods_dir, log_fn=_log)
        mods_dir.mkdir(parents=True, exist_ok=True)

        # Step 2: Link/copy mod files from staging into mods_dir.
        # Reads filemap.txt to know which files belong to which mod.
        # Returns the set of relative paths it placed (used in step 3).
        _, placed = deploy_filemap(filemap, mods_dir, staging,
                                   mode=mode, log_fn=_log,
                                   progress_fn=progress_fn)

        # Step 3: Fill any gaps with the backed-up vanilla files from _Core/.
        # Files already placed by mods (in `placed`) are skipped.
        deploy_core(mods_dir, placed, mode=mode, log_fn=_log)

    def restore(self, log_fn=None) -> None:
        _log = log_fn or (lambda _: None)

        if self._game_path is None:
            raise RuntimeError("Game path is not configured.")

        # TODO: must match the mods_dir used in deploy() above.
        mods_dir = self._game_path / "TODO_mods_dir"
        core_dir = self._game_path / "TODO_mods_dir_Core"

        # Clears mods_dir/ and moves core_dir/ back in its place,
        # returning the game to its pre-deploy vanilla state.
        if core_dir.is_dir():
            restore_data_core(mods_dir, core_dir=core_dir, log_fn=_log)
