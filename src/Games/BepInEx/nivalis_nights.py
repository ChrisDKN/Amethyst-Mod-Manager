"""Nivalis Nights handler."""

from Games.BepInEx.bepinex_base import BepInExBase


class NivalisNights(BepInExBase):
    @property
    def name(self) -> str:
        return "Nivalis Nights"

    @property
    def game_id(self) -> str:
        return "nivalis_nights"

    @property
    def exe_name(self) -> str:
        return "Nivalis Nights.exe"

    @property
    def steam_id(self) -> str:
        return "1488490"

    @property
    def nexus_game_domain(self) -> str:
        return "nivalisnights"

    @property
    def thunderstore_community(self) -> str:
        return "nivalis-nights"