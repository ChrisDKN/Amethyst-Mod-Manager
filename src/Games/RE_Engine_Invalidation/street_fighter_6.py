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
