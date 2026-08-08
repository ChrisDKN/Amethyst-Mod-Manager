"""
deploy.py - façade module.

Shared deployment logic for linking mod files into a game's install directory.
This module used to contain ~3000 lines of implementation; it was split in
2026-04 into mode-specific siblings. All original names (public and private)
are re-exported from here, so existing `from Utils.deploy import X` imports
continue to work unchanged.

Transfer modes (LinkMode enum):
  HARDLINK  - os.link()     No extra disk space; same filesystem required.
  SYMLINK   - os.symlink()  Works across filesystems; dest is a pointer.
  COPY      - shutil.copy2() Full independent copy.

Mode modules:
  Utils.deploy_shared        - primitives, LinkMode, CustomRule, path resolution, snapshots
  Utils.deploy_standard      - Data/ flow: move_to_core, deploy_filemap, deploy_core, restore_data_core
  Utils.deploy_root          - Root_Folder flow: deploy_root_folder, restore_root_folder, …
  Utils.deploy_game_root     - Game-root filemap: deploy_filemap_to_root, restore_filemap_from_root
  Utils.deploy_custom_rules  - CustomRule routing: deploy_custom_rules, restore_custom_rules
  Utils.deploy_wine_dll      - Wine/Proton DLL overrides, remove_deployed_files
"""

from Utils.deploy_shared import *  # noqa: F401,F403
from Utils.deploy_standard import *  # noqa: F401,F403
from Utils.deploy_root import *  # noqa: F401,F403
from Utils.deploy_game_root import *  # noqa: F401,F403
from Utils.deploy_custom_rules import *  # noqa: F401,F403
from Utils.deploy_wine_dll import *  # noqa: F401,F403
