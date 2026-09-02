"""
Utils.deployment - deployment façade package.

Shared deployment logic for linking mod files into a game's install directory.
This module used to contain ~3000 lines of implementation; it was split in
2026-04 into mode-specific siblings. All original names (public and private)
are re-exported from here through the package API.

Transfer modes (LinkMode enum):
  HARDLINK  - os.link()     No extra disk space; same filesystem required.
  SYMLINK   - os.symlink()  Works across filesystems; dest is a pointer.
  COPY      - shutil.copy2() Full independent copy.

Mode modules:
  Utils.deployment.shared        - primitives, LinkMode, CustomRule, path resolution, snapshots
  Utils.deployment.standard      - Data/ flow: move_to_core, deploy_filemap, deploy_core, restore_data_core
  Utils.deployment.root          - Root_Folder flow: deploy_root_folder, restore_root_folder, …
  Utils.deployment.game_root     - Game-root filemap: deploy_filemap_to_root, restore_filemap_from_root
  Utils.deployment.custom_rules  - CustomRule routing: deploy_custom_rules, restore_custom_rules
  Utils.deployment.wine_dll      - Wine/Proton DLL overrides, remove_deployed_files
"""

from Utils.deployment.shared import *  # noqa: F401,F403
from Utils.deployment.standard import *  # noqa: F401,F403
from Utils.deployment.root import *  # noqa: F401,F403
from Utils.deployment.game_root import *  # noqa: F401,F403
from Utils.deployment.custom_rules import *  # noqa: F401,F403
from Utils.deployment.wine_dll import *  # noqa: F401,F403
