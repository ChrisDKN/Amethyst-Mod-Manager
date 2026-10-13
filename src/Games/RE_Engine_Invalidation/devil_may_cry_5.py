"""Devil May Cry 5 game handler."""

from Games.RE_Engine_Invalidation.re_engine_invalidation_base import REEngineInvalidationBase


class DevilMayCry5(REEngineInvalidationBase):

    @property
    def name(self) -> str:
        return "Devil May Cry 5"

    @property
    def game_id(self) -> str:
        return "devil_may_cry_5"

    @property
    def exe_name(self) -> str:
        return "DevilMayCry5.exe"

    @property
    def steam_id(self) -> str:
        return "601150"

    @property
    def nexus_game_domain(self) -> str:
        return "devilmaycry5"
