"""
monster_hunter_wilds.py
Game handler for Monster Hunter Wilds.

Uses the same RE Engine foundation as Resident Evil Requiem:
  - Mods install into the game root
  - Mod authors ship with a reframework/ and/or natives/ top-level folder
  - .pak files routed to game_root/pak_mods/
  - REFramework loads via dinput8.dll
"""

import importlib.util
import sys
from pathlib import Path

# Load ResidentEvilRequiem via file path so the space in the folder name is not
# an issue for the Python import system.
_req_path = Path(__file__).parent.parent / "Resident Evil Requiem" / "resident_evil_requiem.py"
_spec = importlib.util.spec_from_file_location("Games._loaded_resident_evil_requiem", _req_path)
assert _spec is not None and _spec.loader is not None
_mod = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = _mod
_spec.loader.exec_module(_mod)
ResidentEvilRequiem = _mod.ResidentEvilRequiem


class MonsterHunterWilds(ResidentEvilRequiem):

    @property
    def name(self) -> str:
        return "Monster Hunter Wilds"

    @property
    def game_id(self) -> str:
        return "monster_hunter_wilds"

    @property
    def exe_name(self) -> str:
        return "MonsterHunterWilds.exe"

    @property
    def steam_id(self) -> str:
        return "2246340"

    @property
    def nexus_game_domain(self) -> str:
        return "monsterhunterwilds"
    
    @property
    def collections_disabled(self) -> bool:
        return True
    
class MonsterHunterRise(ResidentEvilRequiem):

    @property
    def name(self) -> str:
        return "Monster Hunter Rise"

    @property
    def game_id(self) -> str:
        return "monster_hunter_rise"

    @property
    def exe_name(self) -> str:
        return "MonsterHunterRise.exe"

    @property
    def steam_id(self) -> str:
        return "1446780"

    @property
    def nexus_game_domain(self) -> str:
        return "monsterhunterrise"
    
    @property
    def collections_disabled(self) -> bool:
        return True
