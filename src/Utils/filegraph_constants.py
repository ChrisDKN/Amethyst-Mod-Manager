"""Shared Filegraph status codes and synthetic provider names.

Kept separate from the retired text resolver so UI and install code cannot
accidentally pull legacy map/index machinery into production imports.
"""

CONFLICT_NONE = 0
CONFLICT_WINS = 1
CONFLICT_LOSES = 2
CONFLICT_PARTIAL = 3
CONFLICT_FULL = 4

OVERWRITE_NAME = "[Overwrite]"
ROOT_FOLDER_NAME = "[Root_Folder]"

__all__ = [
    "CONFLICT_NONE", "CONFLICT_WINS", "CONFLICT_LOSES",
    "CONFLICT_PARTIAL", "CONFLICT_FULL", "OVERWRITE_NAME",
    "ROOT_FOLDER_NAME",
]
