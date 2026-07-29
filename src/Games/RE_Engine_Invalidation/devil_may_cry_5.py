"""
devil_may_cry_5.py
Devil May Cry 5 game handler.
"""

from Games.RE_Engine_Invalidation.resident_evil_village import ResidentEvilVillage


class DevilMayCry5(ResidentEvilVillage):

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

    # Profile Groups: disabled for now — only Stardew Valley currently opts
    # in (see base_game.py's profile_groups_supported docstring) pending
    # wider review of the virtual-merge rework. Uncomment to enable once
    # this game's deploy path has been reviewed/tested.
    # @property
    # def profile_groups_supported(self) -> bool:
    #     return True
