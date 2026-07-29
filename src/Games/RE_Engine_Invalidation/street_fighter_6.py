"""
street_fighter_6.py
Street Fighter 6 game handler.
"""

from Games.RE_Engine_Invalidation.resident_evil_village import ResidentEvilVillage


class StreetFighter6(ResidentEvilVillage):

    @property
    def name(self) -> str:
        return "Street Fighter 6"

    @property
    def game_id(self) -> str:
        return "street_fighter_6"

    @property
    def exe_name(self) -> str:
        return "StreetFighter6.exe"

    @property
    def steam_id(self) -> str:
        return "1364780"

    @property
    def nexus_game_domain(self) -> str:
        return "streetfighter6"

    # Profile Groups: disabled for now — only Stardew Valley currently opts
    # in (see base_game.py's profile_groups_supported docstring) pending
    # wider review of the virtual-merge rework. Uncomment to enable once
    # this game's deploy path has been reviewed/tested.
    # @property
    # def profile_groups_supported(self) -> bool:
    #     return True
