"""
monster_hunter_wilds.py
Game handler for Monster Hunter Wilds.

Uses the same RE Engine foundation as Resident Evil Requiem:
  - Mods install into the game root
  - Mod authors ship with a reframework/ and/or natives/ top-level folder
  - .pak files routed to game_root/pak_mods/
  - REFramework loads via dinput8.dll
"""

from Games.RE_Engine_LooseLoading.resident_evil_requiem import ResidentEvilRequiem


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
        return False
