"""
Profile folder structure helper.

Extracted from the old Tk gui/add_game_dialog.py so the Qt UI can create a
game's profile scaffolding without pulling in tkinter. Pure, framework-agnostic.
"""

from __future__ import annotations

from Games.base_game import BaseGame
from Utils.mods.modlist import sync_modlist_with_mods_folder


def create_profile_structure(game: BaseGame) -> None:
    """
    Create the standard profile folder structure for a game if it doesn't exist.

    Profiles/<game.name>/
      mods/           - staging area for installed mods
      overwrite/      - MO2-compatible catch-all for game/tool-generated files
      profiles/
        Profile 1/
          modlist.txt
          plugins.txt
    """
    # get_profile_root() returns the directory that contains mods/, profiles/, etc.
    # - Default: Profiles/<game>/ (mods/ is a subfolder)
    # - Custom staging: the staging path itself is the root
    game_profile_root = game.get_profile_root()
    mods_dir = game.get_mod_staging_path()

    # mods/        - staging area for installed mods
    mods_dir.mkdir(parents=True, exist_ok=True)

    # overwrite/   - MO2-compatible catch-all for files written by the game/tools
    (game_profile_root / "overwrite").mkdir(parents=True, exist_ok=True)

    # Root_Folder/ - files here are deployed to the game's root directory
    (game_profile_root / "Root_Folder").mkdir(parents=True, exist_ok=True)

    # Applications/ - exe files (and shortcuts) to run via Proton
    (game_profile_root / "Applications").mkdir(parents=True, exist_ok=True)

    # profiles/default/  - default profile with empty mod/plugin lists
    profile_dir = game_profile_root / "profiles" / "default"
    profile_dir.mkdir(parents=True, exist_ok=True)
    (profile_dir / "plugins.txt").touch()
    sync_modlist_with_mods_folder(profile_dir / "modlist.txt", mods_dir)
