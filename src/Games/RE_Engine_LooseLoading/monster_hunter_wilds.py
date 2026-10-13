"""Monster Hunter Wilds game handler."""

from Games.RE_Engine_LooseLoading.re_engine_loose_loading_base import REEngineLooseLoadingBase


class MonsterHunterWilds(REEngineLooseLoadingBase):

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
